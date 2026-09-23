"""醒来改代码只提 PR（PLAN_prs §2/§3）：工作副本 + PR 记录 + 工具的真身。

- 工作副本：每个角色一个 git worktree，`<仓根>/.worktrees/<char_id>/`；一个 PR 一条分支
  `wake/<char_id>/<slug>`，从当时的 main 拉出。他不直接敲 git，切分支、提交、重放全在这儿。
- PR 记录：`state/prs/<id>.json`，全局递增编号，**永远留着**（分支会清，记录不删）。
- 角色串台纪律（cassette-charswitch-bug-class）：每个函数显式收 char_id，没有任何
  「当前 PR / 当前工作副本」全局。
- 只有 pr_open 会往分支上提交 → 没有 PR 记录的分支一定没有提交（草稿），切走时可以直接删。
- 合并 / 退回重建 / 重启不在这儿（PLAN_prs §6，第 3、4 步）。

MCP 壳是 prs_mcp.py；所有拒绝都抛 PRError，原话直接给他看，要写清楚该走哪条路。
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import state_store

REPO_ROOT = Path(__file__).resolve().parent.parent
PRS_DIR = state_store.STATE_DIR / "prs"
# 跑测试用主目录的 venv：工作副本是 git 检出，.venv 不在里面
VENV = Path(__file__).resolve().parent / ".venv"
PYTHON = VENV / "bin" / "python"
# 跑测试的沙箱：测试跑的是没人看过的代码（配置里写了为什么、放行了什么）
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_PROFILE = Path(__file__).resolve().parent / "tools" / "pr_test.sb"
MAIN = "main"

MAX_UNFINISHED = 3        # 每个角色同时没办完（待合并+已退回）的 PR 上限（§10.3）
TEST_TIMEOUT_SEC = 300
DIFF_MAX_CHARS = 30000
UNFINISHED = ("open", "reverted")

_CHAR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# 跑测试时只放这几个环境变量进去：后端进程 env 里有 .env 读进来的凭据（沙箱挡文件，挡不了 env）
_TEST_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


class PRError(ValueError):
    """拒绝原话，给他看的。"""


# ---------- 小工具 ----------

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _check_char(char_id: str) -> str:
    if not isinstance(char_id, str) or not _CHAR_RE.match(char_id):
        raise PRError(f"角色 id 不对：{char_id!r}")
    return char_id


def _git_env(char_id: Optional[str] = None) -> dict:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if char_id:
        who, mail = char_id, f"{char_id}@cassette.local"
        env.update(GIT_AUTHOR_NAME=who, GIT_AUTHOR_EMAIL=mail,
                   GIT_COMMITTER_NAME=who, GIT_COMMITTER_EMAIL=mail)
    return env


def _git_run(*args: str, cwd: Optional[Path] = None, char_id: Optional[str] = None,
             input: Optional[bytes] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd or REPO_ROOT, capture_output=True,
                          timeout=120, env=_git_env(char_id), input=input)


def _git(*args: str, cwd: Optional[Path] = None, char_id: Optional[str] = None,
         input: Optional[bytes] = None) -> str:
    r = _git_run(*args, cwd=cwd, char_id=char_id, input=input)
    if r.returncode:
        err = (r.stderr or r.stdout).decode(errors="replace").strip()
        raise PRError(f"git {args[0]} 失败：{err[-400:]}")
    return r.stdout.decode(errors="replace")


@contextmanager
def _flock(name: str):
    PRS_DIR.mkdir(parents=True, exist_ok=True)
    with open(PRS_DIR / name, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------- PR 记录 ----------

def _rec_path(pr_id: int) -> Path:
    return PRS_DIR / f"{int(pr_id)}.json"


def _all_records() -> list[dict]:
    if not PRS_DIR.exists():
        return []
    out = []
    for p in PRS_DIR.glob("*.json"):
        if p.stem.isdigit():
            try:
                out.append(json.loads(p.read_text()))
            except Exception:
                continue
    return out


def _save(rec: dict) -> None:
    PRS_DIR.mkdir(parents=True, exist_ok=True)
    p = _rec_path(rec["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    tmp.replace(p)


def get(pr_id: int) -> dict:
    try:
        return json.loads(_rec_path(pr_id).read_text())
    except (FileNotFoundError, ValueError):
        raise PRError(f"没有 #{pr_id} 这个 PR")


def _record_for_branch(char_id: str, branch: str) -> Optional[dict]:
    hits = [r for r in _all_records() if r["char"] == char_id and r["branch"] == branch]
    return max(hits, key=lambda r: r["id"]) if hits else None


def _unfinished_of(char_id: str) -> list[dict]:
    return [r for r in _all_records() if r["char"] == char_id and r["status"] in UNFINISHED]


# ---------- 工作副本 ----------

def worktree_path(char_id: str) -> Path:
    return REPO_ROOT / ".worktrees" / _check_char(char_id)


def _ensure_worktree(char_id: str) -> Path:
    wt = worktree_path(char_id)
    if (wt / ".git").exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "prune")
    _git("worktree", "add", "--detach", str(wt), MAIN)
    return wt


def _current_branch(wt: Path) -> str:
    r = _git_run("symbolic-ref", "--short", "-q", "HEAD", cwd=wt)
    return r.stdout.decode().strip() if r.returncode == 0 else ""


def _dirty_paths(wt: Path) -> list[str]:
    """工作副本里没提交的路径（含未跟踪、含改名两头）。"""
    raw = _git("status", "--porcelain", "-z", "--untracked-files=all", cwd=wt)
    parts = raw.split("\0")
    out, i = [], 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        out.append(e[3:])
        if e[0] in "RC":           # 改名/复制：下一段是旧路径
            out.append(parts[i])
            i += 1
    return out


def _discard(wt: Path) -> None:
    _git("reset", "--hard", "-q", cwd=wt)
    _git("clean", "-fdq", cwd=wt)


def _branch_exists(branch: str) -> bool:
    return _git_run("rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0


def _merge_base(a: str, b: str) -> str:
    return _git("merge-base", a, b).strip()


def _branch_files(branch: str) -> list[dict]:
    """分支相对它跟 main 的分叉点改了哪些文件：[{path, add, del}]（二进制记 None）。"""
    base = _merge_base(MAIN, branch)
    raw = _git("diff", "--numstat", "-z", "--no-renames", base, branch)
    out = []
    for e in raw.split("\0"):
        if not e:
            continue
        a, d, path = e.split("\t", 2)
        out.append({"path": path,
                    "add": None if a == "-" else int(a),
                    "del": None if d == "-" else int(d)})
    return out


def _skip_worktree_files() -> set[str]:
    """主目录索引里标了 skip-worktree 的文件（本地版带 team id，§2）。"""
    raw = _git("ls-files", "-v", "-z")
    return {e[2:] for e in raw.split("\0") if e[:1] in ("S", "s")}


def _conflict_marked(wt: Path, paths: list[str]) -> list[str]:
    bad = []
    for rel in paths:
        p = wt / rel
        try:
            text = p.read_text(errors="replace")
        except (FileNotFoundError, IsADirectoryError):
            continue
        if re.search(r"^<{7} |^>{7} ", text, re.M):
            bad.append(rel)
    return bad


def _replay_onto_main(wt: Path, char_id: str, branch: str) -> str:
    """分支没跟上 main 时在最新 main 上重放。先 rebase；打架就退回来，把整条分支的改动
    压成一份补丁 3-way 叠到 main 上——冲突留成 <<<<<<< 标记在文件里，他用 Edit 改掉再 pr_open。"""
    if _git_run("merge-base", "--is-ancestor", MAIN, branch).returncode == 0:
        return ""
    if _dirty_paths(wt):       # 下面有 reset --hard，手上没提交的改动不能拿去冒险
        return "main 动过了，但你手上有没提交的改动，这次先不重放：pr_open 提交后再 pr_start 一次。"
    if _git_run("rebase", "-q", MAIN, cwd=wt, char_id=char_id).returncode == 0:
        return "main 动过，已经把你的分支在最新 main 上重放好了。"
    _git_run("rebase", "--abort", cwd=wt)
    base = _merge_base(MAIN, branch)
    patch = _git_run("diff", "--binary", base, branch).stdout
    _git("reset", "--hard", "-q", MAIN, cwd=wt)
    _git_run("apply", "--3way", cwd=wt, input=patch)
    conflicted = _conflict_marked(wt, _dirty_paths(wt))
    if conflicted:
        return ("main 动过，你的改动跟它打架：已经把你的改动叠到最新 main 上，"
                f"这几个文件里留了 <<<<<<< 冲突标记，改好后再 pr_open：{'、'.join(conflicted)}")
    return "main 动过，已经把你的改动叠到最新 main 上（还没提交，pr_open 时一起提）。"


# ---------- 跑测试 ----------

_FAIL_RE = re.compile(r"^(?:FAIL|ERROR): \S+ \((\S+)\)", re.M)
_RAN_RE = re.compile(r"^Ran (\d+) tests?", re.M)


def _run_suite(server_dir: Path) -> dict:
    """在沙箱里跑 server_dir 下的全量测试（tools/pr_test.sb：读写白名单、断网）。
    沙箱起不来就当测试没跑成报出来，**不退回无沙箱跑**。"""
    if not Path(SANDBOX_EXEC).exists():
        return {"ran": 0, "failed": [], "crashed": "沙箱（sandbox-exec）不在，测试没跑"}
    env = {k: os.environ[k] for k in _TEST_ENV_KEYS if k in os.environ}
    try:
        # 每次一个新的临时目录，既当 TMPDIR 又当字节码缓存：沙箱只放行这一个临时目录的写；
        # .pyc 按「源文件 mtime(秒)+大小」判新鲜，同一秒内改成同样长的内容会吃到旧字节码，
        # 测的就不是他刚改的代码
        with tempfile.TemporaryDirectory() as tmp:
            tmp = str(Path(tmp).resolve())
            r = subprocess.run(
                [SANDBOX_EXEC, "-f", str(SANDBOX_PROFILE),
                 "-D", f"VENV={VENV.resolve()}", "-D", f"WT={server_dir.parent.resolve()}",
                 "-D", f"TMP={tmp}",
                 str(PYTHON), "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                cwd=server_dir, capture_output=True, timeout=TEST_TIMEOUT_SEC,
                env=env | {"TMPDIR": tmp + "/", "PYTHONPYCACHEPREFIX": tmp + "/pyc"})
    except subprocess.TimeoutExpired:
        return {"ran": 0, "failed": [], "crashed": f"跑了 {TEST_TIMEOUT_SEC} 秒还没跑完，掐掉了"}
    out = (r.stderr + r.stdout).decode(errors="replace")
    m = _RAN_RE.search(out)
    if not m:
        if "sandbox" in out.lower() and r.returncode:
            return {"ran": 0, "failed": [], "crashed": f"沙箱起不来，测试没跑：{out.strip()[-500:]}"}
        return {"ran": 0, "failed": [], "crashed": out.strip()[-1500:] or "测试没跑起来"}
    return {"ran": int(m.group(1)), "failed": sorted(set(_FAIL_RE.findall(out))), "crashed": ""}


def _baseline(sha: str) -> dict:
    """同一个 main 提交在干净检出里本来就挂的测试。干净检出没有 .env / 角色配置，
    有一批测试天生挂——只有「比基线多挂的」才算这个 PR 弄坏的。按提交缓存。"""
    cache = PRS_DIR / "_baseline" / f"{sha}.json"
    with _flock("_baseline.lock"):
        if cache.exists():
            return json.loads(cache.read_text())
        wt = REPO_ROOT / ".worktrees" / "_baseline"
        if not (wt / ".git").exists():
            wt.parent.mkdir(parents=True, exist_ok=True)
            _git("worktree", "prune")
            _git("worktree", "add", "--detach", str(wt), sha)
        _git("checkout", "-q", "-f", "--detach", sha, cwd=wt)
        _git("clean", "-fdq", cwd=wt)
        res = _run_suite(wt / "server")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(res, ensure_ascii=False))
        return res


def _test_worktree(wt: Path) -> dict:
    head = _git("rev-parse", "HEAD", cwd=wt).strip()
    base = _merge_base(MAIN, head)
    res = _run_suite(wt / "server")
    at = _now()
    if res["crashed"]:
        return {"ok": False, "ran": 0, "new_failed": [], "head": head, "at": at,
                "summary": f"测试没跑完：{res['crashed']}"}
    bl = _baseline(base)
    new = [t for t in res["failed"] if t not in set(bl["failed"])]
    ok = res["ran"] > 0 and not new
    summary = f"跑了 {res['ran']} 条，新挂 {len(new)} 条"
    if bl["failed"]:
        summary += f"（干净检出里本来就挂 {len(bl['failed'])} 条，缺 .env/角色配置所致，不算你的）"
    if new:
        summary += "：" + "、".join(new[:5]) + ("……" if len(new) > 5 else "")
    return {"ok": ok, "ran": res["ran"], "new_failed": new, "head": head, "at": at,
            "summary": summary}


# ---------- 工具真身 ----------

def start(char_id: str, slug: str, discard: bool = False) -> dict:
    """pr_start：切到 wake/<char>/<slug>。新名字 = 从 main 拉新分支；自己开着的 PR 的名字
    = 切回去接着改（main 动过就重放）。"""
    _check_char(char_id)
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise PRError("slug 只能用小写字母、数字和连字符，40 个字以内，比如 fix-wake-log")
    branch = f"wake/{char_id}/{slug}"
    with _flock("_lock"):
        wt = _ensure_worktree(char_id)
        cur = _current_branch(wt)
        dirty = _dirty_paths(wt)
        if dirty and discard:
            _discard(wt)
        elif dirty and cur != branch:
            raise PRError(
                f"工作副本里还有没提交的改动（在 {cur or '无分支'} 上：{'、'.join(dirty[:5])}）。"
                "要留就先 pr_open 提成 PR；不要了就 pr_start 时带 discard=true 丢掉。")
        rec = _record_for_branch(char_id, branch)
        note = ""
        if rec and rec["status"] in ("merged", "closed"):
            raise PRError(f"{slug} 这个名字用过了（#{rec['id']} 已"
                          f"{'合并' if rec['status'] == 'merged' else '关闭'}），换一个。")
        if rec and rec["status"] == "reverted":
            raise PRError(f"#{rec['id']} 被退回了；退回后重修的流程还没上线，先跟机主说一声。")
        if rec:
            if cur != branch:
                _git("checkout", "-q", branch, cwd=wt)
            note = _replay_onto_main(wt, char_id, branch)
        elif cur != branch or not dirty:
            if len(_unfinished_of(char_id)) >= MAX_UNFINISHED:
                ids = "、".join(f"#{r['id']}" for r in _unfinished_of(char_id))
                raise PRError(f"你已经有 {MAX_UNFINISHED} 个没办完的 PR 了（{ids}），"
                              "先收掉一个（pr_close，或者等机主合并）再开新的。")
            _git("checkout", "-q", "-B", branch, MAIN, cwd=wt)
        # 从没开过 PR 的草稿分支上切走：它一定没有提交，删掉免得越攒越多
        if cur and cur != branch and cur.startswith(f"wake/{char_id}/") \
                and not _record_for_branch(char_id, cur):
            _git_run("branch", "-D", cur)
        return {"path": str(wt), "branch": branch,
                "pr": rec["id"] if rec else None,
                "note": note or ("接着改 #%d。" % rec["id"] if rec else
                                 "在这个路径下用 Read/Edit/Write 改，改完 pr_open。")}


def test(char_id: str) -> dict:
    """pr_test：在工作副本里跑一遍后端测试（跑的是没提交的现状）。"""
    wt = worktree_path(char_id)
    if not (wt / ".git").exists():
        raise PRError("还没有工作副本——先 pr_start")
    return _test_worktree(wt)


def open_pr(char_id: str, title: str, description: str) -> dict:
    """pr_open：提交工作副本里的改动 → 跑测试 → 落记录。同一条分支再调 = 更新同一个 PR。"""
    _check_char(char_id)
    title = (title or "").strip()
    description = (description or "").strip()
    if not title:
        raise PRError("标题不能空")
    if not description:
        raise PRError("描述不能空：写给不读代码的人看——做了什么、为什么、合并后机主会感觉到什么变化")
    wt = worktree_path(char_id)
    if not (wt / ".git").exists():
        raise PRError("还没有工作副本——先 pr_start")
    with _flock("_lock"):
        branch = _current_branch(wt)
        if not branch.startswith(f"wake/{char_id}/"):
            raise PRError("工作副本不在你的 PR 分支上——先 pr_start")
        rec = _record_for_branch(char_id, branch)
        if rec and rec["status"] not in UNFINISHED:
            raise PRError(f"这条分支的 #{rec['id']} 已经"
                          f"{'合并' if rec['status'] == 'merged' else '关闭'}了，换个名字重新 pr_start")
        dirty = _dirty_paths(wt)
        conflicted = _conflict_marked(wt, dirty)
        if conflicted:
            raise PRError(f"这几个文件里还有 <<<<<<< 冲突标记，改掉再提：{'、'.join(conflicted)}")
        touched = set(dirty) | {f["path"] for f in _branch_files(branch)}
        guarded = sorted(touched & _skip_worktree_files())
        if guarded:
            raise PRError(f"{'、'.join(guarded)} 在主目录里是带本机配置的本地版，PR 不能碰；"
                          "要改请在聊天里跟机主说。")
        if not dirty and not rec:
            raise PRError("工作副本里没有改动，没东西可提")
        pr_id = rec["id"] if rec else max((r["id"] for r in _all_records()), default=0) + 1
        if dirty:
            _git("add", "-A", "--", *dirty, cwd=wt)
            _git("commit", "-q", "-m", f"{title}\n\n{description}\n\nPR #{pr_id} · {char_id}",
                 cwd=wt, char_id=char_id)
        files = _branch_files(branch)
        if not files:
            raise PRError("分支跟 main 比没有任何改动，没东西可提")
        tests = _test_worktree(wt)
        now = _now()
        new = rec is None
        rec = rec or {"id": pr_id, "char": char_id, "branch": branch, "created_at": now,
                      "merged_at": None, "merge_commit": None, "revert_reason": None}
        rec.update(title=title, description=description, base=_merge_base(MAIN, branch),
                   files=files, touches_ios=any(f["path"].startswith("ios/") for f in files),
                   tests=tests, status="open", updated_at=now)
        _save(rec)
        return {"id": pr_id, "created": new, "tests": tests["summary"], "tests_ok": tests["ok"],
                "files": [f["path"] for f in files]}


def close(char_id: str, pr_id: int, reason: str) -> dict:
    """pr_close：撤回自己的 PR。"""
    _check_char(char_id)
    reason = (reason or "").strip()
    if not reason:
        raise PRError("写一句为什么关")
    with _flock("_lock"):
        rec = get(pr_id)
        if rec["char"] != char_id:
            raise PRError(f"#{pr_id} 不是你提的，只能关自己的")
        if rec["status"] not in UNFINISHED:
            raise PRError(f"#{pr_id} 已经{'合并' if rec['status'] == 'merged' else '关闭'}了")
        now = _now()
        rec.update(status="closed", closed_at=now, closed_by=char_id,
                   close_reason=reason, updated_at=now)
        _save(rec)
        return {"id": rec["id"], "status": "closed"}


_STATUS_ORDER = {"open": 0, "reverted": 0, "merged": 1, "closed": 1}


def list_prs() -> list[dict]:
    """全部 PR：没办完的在前、收口的在后，各自按时间倒序（§5）。"""
    recs = sorted(_all_records(), key=lambda r: r.get("updated_at") or "", reverse=True)
    recs.sort(key=lambda r: _STATUS_ORDER.get(r["status"], 1))
    return [{k: r.get(k) for k in ("id", "char", "title", "status", "updated_at",
                                   "touches_ios")}
            | {"tests_ok": (r.get("tests") or {}).get("ok")} for r in recs]


def view(pr_id: int) -> dict:
    rec = get(pr_id)
    if rec["status"] == "merged" and rec.get("merge_commit"):
        r = _git_run("diff", f"{rec['merge_commit']}^1", rec["merge_commit"])
    elif _branch_exists(rec["branch"]):
        r = _git_run("diff", f"{MAIN}...{rec['branch']}")
    else:
        r = None
    diff = r.stdout.decode(errors="replace") if r is not None and r.returncode == 0 \
        else "（分支已经清理，diff 看不到了）"
    if len(diff) > DIFF_MAX_CHARS:
        diff = diff[:DIFF_MAX_CHARS] + f"\n……（截断，全长 {len(diff)} 字）"
    return rec | {"diff": diff}
