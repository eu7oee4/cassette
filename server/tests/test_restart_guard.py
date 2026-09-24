"""重启看守演练（PLAN_prs §6.2 / §9 第 4 步「先在不上电的端口演练一遍失败回滚」）。

临时 git 仓 + 真的 uvicorn 起一个假后端（只有 /health，随机端口，只听 127.0.0.1），走完整 PR 流：
开 PR → 合并 → 交给 tools/restart_guard.sh 重启。不碰真仓、不碰 :8000、不推 Bark。

- 好的 PR：换上新进程、不退回；新后端「启动做完」后记 live head → 生效 → 分支清掉
- 坏的 PR（lifespan 里炸——预检 import 拦不住的那类）：起不来 → 自动 git revert → 退回后的代码
  再起来 → 退回标记被新后端认领（reverted + 日志尾巴 + 通知他）→ pr_start 同名在最新 main 上重建
- 预检：语法错 / import 错当场拒，不起看守

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_restart_guard -v
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prs
import restart

GUARD = Path(restart.GUARD)
UVICORN = restart.UVICORN

_APP_OK = '''import os
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True, "pid": os.getpid()}
'''

_APP_ROUTE = _APP_OK + '''

@app.get("/hello")
def hello():
    return {"hi": 1}
'''

# 能 import、起到一半才炸：预检拦不住，只能靠看守验活
_APP_BOOM = '''import os
from contextlib import asynccontextmanager
from fastapi import FastAPI


@asynccontextmanager
async def lifespan(_app):
    raise RuntimeError("boom-at-startup")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True, "pid": os.getpid()}
'''

_SUITE = '''import unittest

class T(unittest.TestCase):
    def test_ok(self):
        pass
'''


def _g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:     # 旧进程关到一半回 503
        e.close()
        return None
    except Exception:
        return None


def _alive(pid) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@unittest.skipUnless(UVICORN.exists(), "没有 server/.venv 的 uvicorn")
class GuardRehearsal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.server = self.repo / "server"
        (self.server / "tests").mkdir(parents=True)
        (self.server / "app.py").write_text(_APP_OK)
        (self.server / "tests" / "__init__.py").write_text("")
        (self.server / "tests" / "test_x.py").write_text(_SUITE)
        (self.repo / ".gitignore").write_text("__pycache__/\n.worktrees/\nnohup.out\n")
        _g(self.repo, "init", "-q", "-b", "main")
        _g(self.repo, "add", "-A")
        _g(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self._orig = (prs.REPO_ROOT, prs.PRS_DIR)
        prs.REPO_ROOT = self.repo
        prs.PRS_DIR = self.tmp / "prs"
        self.log = self.server / "nohup.out"
        self.port = _free_port()
        self.pids = []
        # 旧后端：跟真的一样 nohup 起、不是本进程的子进程（不然被杀后是僵尸，kill -0 还当它活着）
        self.old_pid = self._boot()
        prs.record_live_head()

    def tearDown(self):
        h = _health(self.port)
        for pid in self.pids + ([h["pid"]] if h else []):
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        prs.REPO_ROOT, prs.PRS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _boot(self) -> int:
        out = subprocess.run(
            ["/bin/bash", "-c", f"cd '{self.server}'; nohup '{UVICORN}' app:app --host 127.0.0.1 "
                                f"--port {self.port} >> '{self.log}' 2>&1 < /dev/null & echo $!"],
            capture_output=True, text=True, check=True).stdout
        pid = int(out.strip())
        self.pids.append(pid)
        for _ in range(100):
            h = _health(self.port)
            if h and h["pid"] == pid:
                return pid
            time.sleep(0.1)
        self.fail("假后端没起来：" + self.log.read_text()[-500:])

    def _merge_pr(self, slug, app_text) -> int:
        prs.start("default", slug)
        (prs.worktree_path("default") / "server" / "app.py").write_text(app_text)
        pr_id = prs.open_pr("default", f"改 {slug}", "演练")["id"]
        prs.merge(pr_id, force=True)
        return pr_id

    def _run_guard(self) -> subprocess.CompletedProcess:
        (prs.PRS_DIR / "_restarting").write_text("x")
        env = dict(os.environ,
                   GUARD_REPO=str(self.repo), GUARD_SERVER_DIR=str(self.server),
                   GUARD_UVICORN=str(UVICORN), GUARD_HOST="127.0.0.1", GUARD_PORT=str(self.port),
                   GUARD_OLD_PID=str(self.old_pid),
                   GUARD_REVERTS=" ".join(f"{i}:{s}" for i, s in prs.pending_merges()),
                   GUARD_STATE=str(prs.PRS_DIR), GUARD_LOG=str(self.log),
                   GUARD_HEALTH_SEC="20", GUARD_BARK="")
        return subprocess.run(["/bin/bash", str(GUARD)], env=env, capture_output=True, errors="replace",
                              text=True, timeout=120)

    def test_good_pr_restarts_and_branch_cleaned_after_live(self):
        pr_id = self._merge_pr("hello", _APP_ROUTE)
        self.assertEqual([i for i, _ in prs.pending_merges()], [pr_id])
        r = self._run_guard()
        self.assertEqual(r.returncode, 0, self.log.read_text()[-800:])
        h = _health(self.port)
        self.assertIsNotNone(h)
        self.assertNotEqual(h["pid"], self.old_pid)
        self.assertFalse(_alive(self.old_pid))
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/hello", timeout=2) as resp:
            self.assertEqual(json.loads(resp.read()), {"hi": 1})
        self.assertFalse((prs.PRS_DIR / "_rollback").exists())
        self.assertFalse((prs.PRS_DIR / "_restarting").exists())
        self.assertEqual(prs.get(pr_id)["status"], "merged")
        # 合了还没生效之前分支不删；新后端启动做完记 live head 之后 = 验过活，才删
        branch = prs.get(pr_id)["branch"]
        prs.start("default", "other")          # 工作副本切走，分支才删得掉
        self.assertEqual(prs.cleanup_branches(), [])
        prs.record_live_head()
        self.assertEqual(prs.pending_merges(), [])
        self.assertEqual(prs.cleanup_branches(), [branch])

    def test_broken_pr_reverted_and_back_up(self):
        good_head = _g(self.repo, "rev-parse", "HEAD").strip()
        pr_id = self._merge_pr("boom", _APP_BOOM)
        self.assertEqual(prs.merge_blockers(pr_id), ["这个 PR 现在是「merged」，不是待合并"])
        t0 = time.time()
        r = self._run_guard()
        self.assertEqual(r.returncode, 0, self.log.read_text()[-1500:])
        self.assertLess(time.time() - t0, 60, "起不来应当是进程一死就判，不是干等到超时")
        # 退回：主目录 HEAD 是一个 revert 提交，app.py 回到好的版本，旧的合并还在历史里（没 reset）
        self.assertEqual((self.server / "app.py").read_text(), _APP_OK)
        self.assertIn("Revert", _g(self.repo, "log", "-1", "--format=%s"))
        _g(self.repo, "merge-base", "--is-ancestor", good_head, "HEAD")
        _g(self.repo, "merge-base", "--is-ancestor", prs.get(pr_id)["merge_commit"], "HEAD")
        # 用退回后的代码起来了
        h = _health(self.port)
        self.assertIsNotNone(h)
        self.assertNotEqual(h["pid"], self.old_pid)
        self.assertIn("[guard", self.log.read_text())
        # 新后端启动时认领标记
        self.assertEqual(prs.settle_rollback(), [pr_id])
        rec = prs.get(pr_id)
        self.assertEqual(rec["status"], "reverted")
        self.assertIn("boom-at-startup", rec["revert_reason"])
        self.assertNotIn("[guard", rec["revert_reason"])
        self.assertEqual(rec["revert_commit"], _g(self.repo, "rev-parse", "HEAD").strip())
        self.assertFalse((prs.PRS_DIR / "_rollback").exists())
        ev = prs.take_events("default")
        self.assertTrue(any("已被自动退回" in e for e in ev), ev)
        self.assertEqual(prs.settle_rollback(), [])       # 只认领一次
        prs.record_live_head()
        self.assertEqual(prs.pending_merges(), [])        # 退回的不再算待生效
        # 他 pr_start 同名：在最新 main（含 revert）上重建，被退回的改动原样回来
        out = prs.start("default", "boom")
        self.assertEqual(out["branch"], "wake/default/boom-r1")
        self.assertEqual(out["pr"], pr_id)
        wt_app = prs.worktree_path("default") / "server" / "app.py"
        self.assertEqual(wt_app.read_text(), _APP_BOOM)
        self.assertEqual(prs._dirty_paths(prs.worktree_path("default")), [])
        self.assertFalse(prs._branch_exists("wake/default/boom"))
        # 再调一次不重建，接着改
        self.assertEqual(prs.start("default", "boom")["branch"], "wake/default/boom-r1")
        # 修好 pr_open → 回到待合并，合得进去
        wt_app.write_text(_APP_ROUTE)
        prs.open_pr("default", "改 boom（修好）", "修了启动")
        rec = prs.get(pr_id)
        self.assertEqual((rec["status"], rec["branch"]), ("open", "wake/default/boom-r1"))
        self.assertEqual(prs.merge_blockers(pr_id), [])
        prs.merge(pr_id, force=True)
        self.assertEqual((self.server / "app.py").read_text(), _APP_ROUTE)

    def test_nothing_to_revert_gives_up_loudly(self):
        # 主目录自己坏了（不是 PR 的事）：不乱退，报失败
        (self.server / "app.py").write_text(_APP_BOOM)
        _g(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "owner breaks")
        r = self._run_guard()
        self.assertEqual(r.returncode, 1)
        self.assertIn("没有待生效的 PR", self.log.read_text())
        self.assertFalse((prs.PRS_DIR / "_rollback").exists())
        self.assertFalse((prs.PRS_DIR / "_restarting").exists())


class Preflight(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = restart.SERVER_DIR

    def tearDown(self):
        restart.SERVER_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check(self, files: dict):
        for name, text in files.items():
            (self.tmp / name).write_text(text)
        restart.SERVER_DIR = self.tmp
        restart.preflight()

    def test_ok(self):
        self._check({"app.py": _APP_OK})

    def test_syntax_error_in_lazily_imported_module(self):
        with self.assertRaises(restart.RestartError) as cm:
            self._check({"app.py": _APP_OK, "lazy.py": "def f(:\n"})
        self.assertIn("lazy.py", str(cm.exception))

    def test_import_error(self):
        with self.assertRaises(restart.RestartError) as cm:
            self._check({"app.py": "import no_such_module_xyz\n"})
        self.assertIn("no_such_module_xyz", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
