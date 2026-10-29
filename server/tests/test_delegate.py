"""派活提 PR（PLAN_delegate §3 / §6，10-30 第 7 步）：校验（并发 / 上限 / 白名单 / slug 复用）、
四种结局的判定与事件文案、停下来问 → 回答 → resume、重启扫尾、门（副本外 / .git / 相对路径）、
派活副本上提 PR 的记录带 delegated。全部在临时 git 仓里跑；真正起 session 的 _run_session 打桩。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_delegate -v
"""
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import delegate
import prs
import session_mgr as sm

_SUITE = '''import unittest
import mod

class T(unittest.TestCase):
    def test_value(self):
        self.assertEqual(mod.VALUE, 1)
'''


def _g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


class DelegateBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / "server" / "tests").mkdir(parents=True)
        (self.repo / "server" / "mod.py").write_text("VALUE = 1\n")
        (self.repo / "server" / "notes.txt").write_text("a\n")
        (self.repo / "server" / "tests" / "__init__.py").write_text("")
        (self.repo / "server" / "tests" / "test_mod.py").write_text(_SUITE)
        (self.repo / ".gitignore").write_text("__pycache__/\n.worktrees/\nstate/\n")
        _g(self.repo, "init", "-q", "-b", "main")
        _g(self.repo, "add", "-A")
        _g(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self._orig = (prs.REPO_ROOT, prs.PRS_DIR, prs.BARK_ON_OPEN, delegate.BARK,
                      delegate._run_session, prs._test_worktree, config.DELEGATE_DAILY,
                      config.DELEGATE_DAILY_WAKE)
        prs.BARK_ON_OPEN = False
        delegate.BARK = False
        prs.REPO_ROOT = self.repo
        prs.PRS_DIR = self.tmp / "prs"
        prs._test_worktree = lambda wt: {"ok": True, "summary": "（桩）全绿", "ran": 1}
        config.DELEGATE_DAILY = 5
        config.DELEGATE_DAILY_WAKE = 2
        delegate._tasks.clear()
        self.sessions: list[dict] = []        # 每段 session 收到的 (opts, message)
        self.script: list = []               # 预设的每段结果

        async def fake_run(opts, message):
            self.sessions.append({"opts": opts, "message": message})
            step = self.script.pop(0) if self.script else {"kind": "ask", "text": "卡在哪？"}
            if callable(step):
                step = await step(opts)
            kind = step.get("kind")
            res = SimpleNamespace(is_error=False, subtype="success", usage={"input_tokens": 1},
                                  session_id="sid-1")
            if kind == "open":
                # 模型改了文件 + 调了 pr_open（真跑 open_pr，桩只跳过 SDK 和 MCP 壳）
                wt = Path(opts.cwd)
                (wt / "server" / "notes.txt").write_text(step.get("text", "changed\n"))
                key = wt.name
                char, slug = key.split("~", 1)
                r = await asyncio.to_thread(delegate.open_pr, char, slug, "改 notes", "让机主看到的变化")
                delegate._sinks[key]["pr_id"] = r["id"]
                return {"session_id": "sid-1", "result": res, "last_text": "提好了", "error": None}
            if kind == "ask":
                return {"session_id": "sid-1", "result": res, "last_text": step["text"], "error": None}
            if kind == "error":
                res.is_error, res.subtype = True, "error_during_execution"
                return {"session_id": "sid-1", "result": res, "last_text": step.get("text", ""), "error": None}
            if kind == "dead":
                return {"session_id": "sid-1", "result": None, "last_text": "", "error": None}
            if kind == "exc":
                return {"session_id": None, "result": None, "last_text": "", "error": "Boom: x"}
            raise AssertionError(kind)
        delegate._run_session = fake_run
        # 事件循环：IsolatedAsyncioTestCase 的 loop 在 asyncSetUp 里才有，start() 走 _schedule
        # 要 session_mgr.event_loop()——在 asyncSetUp 喂。

    async def asyncSetUp(self):
        self._loop_orig = sm._LOOP
        sm.set_event_loop(asyncio.get_running_loop())

    async def asyncTearDown(self):
        for t in list(delegate._tasks.values()):
            t.cancel()
        await asyncio.sleep(0)
        sm._LOOP = self._loop_orig

    def tearDown(self):
        (prs.REPO_ROOT, prs.PRS_DIR, prs.BARK_ON_OPEN, delegate.BARK, delegate._run_session,
         prs._test_worktree, config.DELEGATE_DAILY, config.DELEGATE_DAILY_WAKE) = self._orig
        delegate._tasks.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def settle(self):
        """等挂上去的段跑完。"""
        for _ in range(200):
            await asyncio.sleep(0.01)
            if not delegate._tasks:
                return
        raise AssertionError(f"派活段没收尾：{list(delegate._tasks)}")

    BRIEF = "要解决：notes.txt 改成 changed。位置：server/notes.txt:1。不能动：测试。做完：pr_test 过、pr_open 提上去。"


class StartAndOutcomeTest(DelegateBase):
    async def test_open_pr_outcome(self):
        self.script = [{"kind": "open"}]
        r = delegate.start("cass", "notes-zh", "sonnet", self.BRIEF)
        self.assertEqual(r["status"], "running")
        self.assertTrue((self.repo / ".worktrees" / "cass~notes-zh" / ".git").exists())
        self.assertEqual(prs.worktree_owner((self.repo / ".worktrees" / "cass~notes-zh" / "x").resolve()),
                         "cass~notes-zh")                           # 他本人的门会当「别人的副本」拒
        await self.settle()
        rec = delegate._load("cass", "notes-zh")
        self.assertEqual(rec["status"], "opened")
        self.assertEqual(rec["pr_id"], 1)
        pr = prs.get(1)
        self.assertEqual(pr["char"], "cass")
        self.assertEqual(pr["branch"], "wake/cass/notes-zh")
        self.assertEqual(pr["delegated"]["model"], "sonnet")
        self.assertIn("server/notes.txt", [f["path"] for f in pr["files"]])
        self.assertFalse((self.repo / ".worktrees" / "cass~notes-zh").exists())   # 副本拆了
        self.assertTrue(prs._branch_exists("wake/cass/notes-zh"))                 # 分支留着
        ev = prs.take_events("cass")
        self.assertEqual(len(ev), 1)
        self.assertIn("#1", ev[0])
        self.assertIn("sonnet", ev[0])
        # 首段消息就是说明原文；系统提示带副本路径和分支
        self.assertEqual(self.sessions[0]["message"], self.BRIEF)
        self.assertIn("wake/cass/notes-zh", self.sessions[0]["opts"].system_prompt)
        self.assertEqual(self.sessions[0]["opts"].model, "sonnet")
        self.assertTrue(self.sessions[0]["opts"].strict_mcp_config)
        self.assertNotIn("Bash", self.sessions[0]["opts"].tools)

    async def test_ask_then_reply_resumes(self):
        import wake_sdk
        woke = []
        orig = wake_sdk.enqueue_wake
        wake_sdk.enqueue_wake = lambda cid, trigger, note="", force=False: woke.append((cid, trigger)) or True
        try:
            self.script = [{"kind": "ask", "text": "格式要哪种？"}, {"kind": "open"}]
            delegate.start("cass", "fmt", "opus", self.BRIEF)
            await self.settle()
            rec = delegate._load("cass", "fmt")
            self.assertEqual(rec["status"], "asking")
            self.assertEqual(rec["question"], "格式要哪种？")
            self.assertEqual(rec["session_id"], "sid-1")
            self.assertEqual(woke, [("cass", "delegate")])
            self.assertTrue((self.repo / ".worktrees" / "cass~fmt").exists())   # 副本留着
            ev = prs.take_events("cass")
            self.assertIn("停下来问你：格式要哪种？", ev[0])
            self.assertIn("pr_delegate_reply", ev[0])
            # 还没回答就再派：同角色一个在跑才拒；asking 不算在跑，但算没办完
            with self.assertRaises(prs.PRError):
                delegate.reply("cass", "fmt", "")
            delegate.reply("cass", "fmt", "用中文逗号")
            await self.settle()
            rec = delegate._load("cass", "fmt")
            self.assertEqual(rec["status"], "opened")
            self.assertEqual(rec["qa"][0]["a"], "用中文逗号")
            self.assertEqual(self.sessions[1]["message"], "用中文逗号")
            self.assertEqual(self.sessions[1]["opts"].resume, "sid-1")
            self.assertEqual(prs.get(1)["delegated"]["qa"][0]["q"], "格式要哪种？")
        finally:
            wake_sdk.enqueue_wake = orig

    async def test_error_and_exception_fail_and_release_slug(self):
        for i, step in enumerate(({"kind": "error", "text": "炸了"}, {"kind": "exc"})):
            self.script = [step]
            delegate.start("cass", f"bad{i}", "haiku", self.BRIEF)
            await self.settle()
            rec = delegate._load("cass", f"bad{i}")
            self.assertEqual(rec["status"], "failed", step)
            self.assertFalse(prs._branch_exists(f"wake/cass/bad{i}"))      # 分支删了
            self.assertFalse((self.repo / ".worktrees" / f"cass~bad{i}").exists())
            ev = prs.take_events("cass")
            self.assertIn("没做成", ev[0])
        # 失败后同一个 slug 可以再派
        self.script = [{"kind": "open"}]
        delegate.start("cass", "bad0", "haiku", self.BRIEF)
        await self.settle()
        self.assertEqual(delegate._load("cass", "bad0")["status"], "opened")

    async def test_dead_stream_stays_running_and_sweep_resumes_once(self):
        self.script = [{"kind": "dead"}]
        delegate.start("cass", "dead", "sonnet", self.BRIEF)
        await self.settle()
        rec = delegate._load("cass", "dead")
        self.assertEqual(rec["status"], "running")                 # 不判，留给扫尾
        # 启动扫尾：resume 一次
        self.script = [{"kind": "open"}]
        done = await delegate.sweep_on_start()
        self.assertEqual(done, ["cass~dead:resumed"])
        await self.settle()
        self.assertEqual(self.sessions[-1]["message"], delegate.RESUME_NOTE)
        self.assertEqual(self.sessions[-1]["opts"].resume, "sid-1")
        self.assertEqual(delegate._load("cass", "dead")["status"], "opened")

    async def test_sweep_fails_second_restart(self):
        self.script = [{"kind": "dead"}]
        delegate.start("cass", "dead2", "sonnet", self.BRIEF)
        await self.settle()
        rec = delegate._load("cass", "dead2")
        rec["resumed_after_restart"] = 1
        delegate._save(rec)
        done = await delegate.sweep_on_start()
        self.assertEqual(done, ["cass~dead2:failed"])
        self.assertEqual(delegate._load("cass", "dead2")["status"], "failed")

    async def test_cancel_asking(self):
        self.script = [{"kind": "ask", "text": "？"}]
        import wake_sdk
        orig = wake_sdk.enqueue_wake
        wake_sdk.enqueue_wake = lambda *a, **k: True
        try:
            delegate.start("cass", "c1", "sonnet", self.BRIEF)
            await self.settle()
        finally:
            wake_sdk.enqueue_wake = orig
        prs.take_events("cass")
        delegate.cancel("cass", "c1", "不要了")
        rec = delegate._load("cass", "c1")
        self.assertEqual(rec["status"], "cancelled")
        self.assertFalse((self.repo / ".worktrees" / "cass~c1").exists())
        self.assertIn("撤回了", prs.take_events("cass")[0])
        with self.assertRaises(prs.PRError):
            delegate.cancel("cass", "c1")


class ValidationTest(DelegateBase):
    async def test_model_whitelist_and_alias(self):
        self.assertEqual(delegate.resolve_model("fable"), "claude-fable-5-1")
        for bad in ("gpt-5", "", "Sonnet"):
            with self.assertRaises(prs.PRError):
                delegate.resolve_model(bad)

    async def test_one_running_per_char_and_daily_caps(self):
        hold = asyncio.Event()

        async def wait_then_open(opts):
            await hold.wait()
            return {"kind": "open"}
        self.script = [wait_then_open]
        delegate.start("cass", "a1", "sonnet", self.BRIEF)
        await asyncio.sleep(0.02)
        with self.assertRaises(prs.PRError) as cm:
            delegate.start("cass", "a2", "sonnet", self.BRIEF)
        self.assertIn("在跑", str(cm.exception))
        hold.set()
        await self.settle()
        # 日上限：醒来 2 次
        for i in range(2):
            self.script = [{"kind": "open"}]
            delegate.start("cass", f"w{i}", "haiku", self.BRIEF, origin="wake")
            await self.settle()
            prs.close("cass", delegate._load("cass", f"w{i}")["pr_id"], "收掉")   # 别撞 3 个上限
        with self.assertRaises(prs.PRError) as cm:
            delegate.start("cass", "w9", "haiku", self.BRIEF, origin="wake")
        self.assertIn("上限", str(cm.exception))
        self.script = [{"kind": "open"}]
        delegate.start("cass", "c9", "haiku", self.BRIEF, origin="chat")   # 聊天档另算
        await self.settle()

    async def test_gray_release_gate(self):
        orig = config.DELEGATE_CHARS
        config.DELEGATE_CHARS = {"cass"}
        try:
            with self.assertRaises(prs.PRError) as cm:
                delegate.start("default", "x1", "sonnet", self.BRIEF)
            self.assertIn("灰度", str(cm.exception))
            self.script = [{"kind": "open"}]
            delegate.start("cass", "x1", "sonnet", self.BRIEF)
            await self.settle()
        finally:
            config.DELEGATE_CHARS = orig

    async def test_slug_reuse_and_short_brief(self):
        with self.assertRaises(prs.PRError):
            delegate.start("cass", "x", "sonnet", "太短")
        prs.start("cass", "mine")
        with self.assertRaises(prs.PRError):
            delegate.start("cass", "Bad Slug", "sonnet", self.BRIEF)
        # 他自己 pr_start 过但没提的草稿分支名：prs 没记录，能派（分支会被重建）
        self.script = [{"kind": "open"}]
        delegate.start("cass", "mine2", "sonnet", self.BRIEF)
        await self.settle()
        with self.assertRaises(prs.PRError) as cm:
            delegate.start("cass", "mine2", "sonnet", self.BRIEF)
        self.assertIn("用过了", str(cm.exception))


class GateTest(unittest.IsolatedAsyncioTestCase):
    """门：写只在副本内（相对路径按副本解析）、.git 拒、Bash 拒；读走 readonly_path_guard。"""

    def setUp(self):
        import pipeline
        self.wt = Path(tempfile.mkdtemp())
        self.gate = delegate._gate("cass", self.wt)

    def tearDown(self):
        shutil.rmtree(self.wt, ignore_errors=True)

    def dec(self, out):
        return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision", "allow")

    async def test_write_inside_only(self):
        self.assertEqual(self.dec(await self.gate({"tool_name": "Edit", "tool_input": {"file_path": "server/x.py"}}, "t", None)), "allow")
        self.assertEqual(self.dec(await self.gate({"tool_name": "Write", "tool_input": {"file_path": str(self.wt / "a.py")}}, "t", None)), "allow")
        self.assertEqual(self.dec(await self.gate({"tool_name": "Edit", "tool_input": {"file_path": "../x.py"}}, "t", None)), "deny")
        self.assertEqual(self.dec(await self.gate({"tool_name": "Edit", "tool_input": {"file_path": "/etc/hosts"}}, "t", None)), "deny")
        self.assertEqual(self.dec(await self.gate({"tool_name": "Edit", "tool_input": {"file_path": ".git/config"}}, "t", None)), "deny")
        self.assertEqual(self.dec(await self.gate({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t", None)), "deny")

    async def test_read_uses_char_guard(self):
        import pipeline
        ok = await self.gate({"tool_name": "Read",
                              "tool_input": {"file_path": str(pipeline.CODE_ROOT / "README.md")}}, "t", None)
        self.assertEqual(self.dec(ok), "allow")
        bad = await self.gate({"tool_name": "Read",
                               "tool_input": {"file_path": str(pipeline.CODE_ROOT / "server" / ".env")}}, "t", None)
        self.assertEqual(self.dec(bad), "deny")
        rel = await self.gate({"tool_name": "Read", "tool_input": {"file_path": "server/x.py"}}, "t", None)
        self.assertEqual(self.dec(rel), "allow")        # 相对路径按副本解析，副本里的随便读
        env = await self.gate({"tool_name": "Read", "tool_input": {"file_path": "server/.env"}}, "t", None)
        self.assertEqual(self.dec(env), "deny")


class RowsTest(DelegateBase):
    async def test_rows_and_running_now(self):
        import characters
        orig = characters.display_name
        characters.display_name = lambda c: "Cassius"
        try:
            hold = asyncio.Event()

            async def wait_then_open(opts):
                await hold.wait()
                return {"kind": "open"}
            self.script = [wait_then_open]
            delegate.start("cass", "r1", "sonnet", self.BRIEF)
            await asyncio.sleep(0.02)
            rows = delegate.rows("cass")
            self.assertEqual(rows[0]["status"], "running")
            self.assertEqual(rows[0]["kind"], "delegate")
            self.assertIn("在跑", delegate.running_now()[0])
            hold.set()
            await self.settle()
            self.assertEqual(delegate.rows("cass")[0]["status"], "opened")
            self.assertEqual(delegate.running_now(), [])
        finally:
            characters.display_name = orig


if __name__ == "__main__":
    unittest.main()
