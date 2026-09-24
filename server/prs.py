"""醒来改代码只提 PR（PLAN_prs §2/§3）：工作副本 + PR 记录 + 工具的真身。

- 工作副本：每个角色一个 git worktree，`<仓根>/.worktrees/<char_id>/`；一个 PR 一条分支
  `wake/<char_id>/<slug>`，从当时的 main 拉出。他不直接敲 git，切分支、提交、重放全在这儿。
- PR 记录：`state/prs/<id>.json`，全局递增编号，**永远留着**（分支会清，记录不删）。
- 角色串台纪律（cassette-charswitch-bug-class）：每个函数显式收 char_id，没有任何
  「当前 PR / 当前工作副本」全局。
- 只有 pr_open 会往分支上提交 → 没有 PR 记录的分支一定没有提交（草稿），切走时可以直接删。
- 合并 / 机主关闭在这儿（§6.1，REST 调）；重启后端的看守在 restart.py + tools/restart_guard.sh，
  这儿管它前后两头：交给它「起不来要退哪几个合并」、新后端启动时认领它的退回标记（§6.2）。
- 被退回的 PR 由 pr_start 同名重建：从最新 main 拉 `<分支>-r<N>`，把合并带进来的改动重新叠上（§3）。

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


def _record_for_slug(char_id: str, slug: str) -> Optional[dict]:
    """按 pr_start 的名字找 PR：分支是 wake/<char>/<slug>，或被退回重建后的 …-r<N>。"""
    pat = re.compile(re.escape(f"wake/{char_id}/{slug}") + r"(?:-r\d+)?")
    hits = [r for r in _all_records() if r["char"] == char_id and pat.fullmatch(r["branch"])]
    return max(hits, key=lambda r: r["id"]) if hits else None


def _unfinished_of(char_id: str) -> list[dict]:
    return [r for r in _all_records() if r["char"] == char_id and r["status"] in UNFINISHED]


# ---------- 工作副本 ----------

def worktree_path(char_id: str) -> Path:
    return REPO_ROOT / ".worktrees" / _check_char(char_id)


def worktree_owner(p: Path) -> Optional[str]:
    """一条**已 resolve** 的路径落在谁的工作副本里：角色 id（或 "_baseline"）；
    落在 .worktrees/ 本身算 ""；不在任何副本里 → None。门（chat_loop._wake_gate）用。"""
    root = (REPO_ROOT / ".worktrees").resolve()
    if not p.is_relative_to(root):
        return None
    parts = p.relative_to(root).parts
    return parts[0] if parts else ""


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


def _rebuild_reverted(wt: Path, char_id: str, rec: dict) -> str:
    """被退回的 PR 重新开工（§3「已退回」）。不能把旧分支再合一次：git 认为那些提交已经合过了，
    只带新提交进来，被 revert 掉的那部分会悄悄丢。所以从当前 main 拉新分支 …-r<N>，
    把当初合并带进来的完整改动（merge_commit 对它第一个父提交的 diff）重新叠上去。
    PR 编号不变，记录里换 branch；状态留在「已退回」，他 pr_open 时回到「待合并」。"""
    if _dirty_paths(wt):
        raise PRError(f"#{rec['id']} 被退回了，要在最新 main 上重建，但工作副本里还有没提交的改动。"
                      "pr_start 带 discard=true 再调一次（被退回的改动会从合并记录里原样找回来）。")
    m = re.fullmatch(r"(.*?)(?:-r(\d+))?", rec["branch"])
    new = f"{m.group(1)}-r{int(m.group(2) or 0) + 1}"
    patch = _git_run("diff", "--binary", f"{rec['merge_commit']}^1", rec["merge_commit"]).stdout
    _git("checkout", "-q", "-B", new, MAIN, cwd=wt)
    _git_run("apply", "--3way", cwd=wt, input=patch)
    conflicted = _conflict_marked(wt, _dirty_paths(wt))
    if conflicted:
        note = ("已经把 #%d 被退回的改动叠到最新 main 上，但跟 main 后来的改动打架，这几个文件里留了 "
                "<<<<<<< 冲突标记，改好后连同修复一起 pr_open：%s" % (rec["id"], "、".join(conflicted)))
    else:
        dirty = _dirty_paths(wt)
        if dirty:
            _git("add", "-A", "--", *dirty, cwd=wt)
            _git("commit", "-q", "-m", f"重建 #{rec['id']}：{rec['title']}（被退回的改动）",
                 cwd=wt, char_id=char_id)
        note = ("已经把 #%d 被退回的改动原样重新叠到最新 main 上了。看 pr_view 里的 revert_reason"
                "（后端起不来时的日志），修好后 pr_open 更新这个 PR。" % rec["id"])
    old = rec["branch"]
    if old != new and _branch_exists(old):
        _git_run("branch", "-D", old)
    rec.update(branch=new, rebuilt_for=rec.get("revert_commit"), updated_at=_now())
    _save(rec)
    return note


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
        rec = _record_for_slug(char_id, slug)
        if rec:
            branch = rec["branch"]      # 被退回重建过的 PR 分支名带 -r<N>
        if dirty and not discard and cur != branch:
            raise PRError(
                f"工作副本里还有没提交的改动（在 {cur or '无分支'} 上：{'、'.join(dirty[:5])}）。"
                "要留就先 pr_open 提成 PR；不要了就 pr_start 时带 discard=true 丢掉。")
        note = ""
        if rec and rec["status"] in ("merged", "closed"):
            raise PRError(f"{slug} 这个名字用过了（#{rec['id']} 已"
                          f"{'合并' if rec['status'] == 'merged' else '关闭'}），换一个。")
        if rec and rec["status"] == "reverted" and rec.get("rebuilt_for") != rec.get("revert_commit"):
            note = _rebuild_reverted(wt, char_id, rec)
            branch = rec["branch"]
        elif rec:
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


# ---------- 生效没有（现算，不进通知）----------
# 「重启了没有」是会变的状态：写进一条只说一次的通知，重启之后那句话就停在他记忆里骗他。
# 所以通知只报事件（合并了/被关了/合不进去），生效没有由这里当场算：后端每次启动记下
# 自己加载的是哪个提交（_live_head），合并提交在它的祖先里 = 正在跑的代码里有它。

def record_live_head() -> None:
    """后端启动做完时调（app._lifespan 的 yield 前）：记下这次进程加载的代码是哪个提交。
    写在启动最后一步，所以它同时就是「上一次正常起来的代码」（§6.2 的 last_good）：
    起不来的进程走不到这儿，不会把坏提交记成好的。"""
    try:
        head = _git("rev-parse", "HEAD").strip()
        PRS_DIR.mkdir(parents=True, exist_ok=True)
        (PRS_DIR / "_live_head").write_text(head)
    except Exception:
        pass


def effective(rec: dict) -> Optional[bool]:
    """合并了的 PR 此刻在不在正在跑的代码里。不是 merged / 不知道（没记过启动）→ None。"""
    if rec.get("status") != "merged" or not rec.get("merge_commit"):
        return None
    try:
        live = (PRS_DIR / "_live_head").read_text().strip()
    except FileNotFoundError:
        return None
    return _git_run("merge-base", "--is-ancestor", rec["merge_commit"], live).returncode == 0


# ---------- 给他的通知（事件，下一轮开头出现一次）----------

def _event(char_id: str, text: str) -> None:
    d = PRS_DIR / "_events"
    d.mkdir(parents=True, exist_ok=True)
    with _flock("_events.lock"), open(d / f"{char_id}.jsonl", "a") as f:
        f.write(json.dumps({"ts": _now(), "text": text}, ensure_ascii=False) + "\n")


def take_events(char_id: str) -> list[str]:
    """取走这个角色攒着的 PR 事件（取一次即消费）。注入组装时调。"""
    p = PRS_DIR / "_events" / f"{_check_char(char_id)}.jsonl"
    if not p.exists():
        return []
    with _flock("_events.lock"):
        try:
            lines = p.read_text().splitlines()
            p.unlink()
        except FileNotFoundError:
            return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln)["text"])
        except Exception:
            continue
    return out


# ---------- 合并 / 机主关闭（REST 调，PLAN_prs §6.1）----------

class NeedsForce(PRError):
    """测试没过：机主在 app 上二次确认「强行合并」才合。"""


def _main_dirty() -> set[str]:
    raw = _git("status", "--porcelain", "-z", "--untracked-files=all")
    parts, out, i = raw.split("\0"), set(), 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        out.add(e[3:])
        if e[0] in "RC":
            out.add(parts[i])
            i += 1
    return out


def _blockers(rec: dict) -> tuple[list[str], bool]:
    """挡住合并的原因（机主看的原话），以及其中有没有「跟 main 打架」（要通知他）。"""
    if rec["status"] != "open":
        return [f"这个 PR 现在是「{rec['status']}」，不是待合并"], False
    if not _branch_exists(rec["branch"]):
        return ["PR 的分支找不到了"], False
    out, conflict = [], False
    cur = _git_run("symbolic-ref", "--short", "-q", "HEAD").stdout.decode().strip()
    if cur != MAIN:
        out.append(f"主目录现在在 {cur or '游离的提交'} 上，不在 {MAIN}——切回 {MAIN} 再合")
    if _git_run("diff", "--cached", "--quiet").returncode != 0:
        out.append("主目录暂存区里有东西（git add 过没提交）——合并会把它一起带进去，先处理掉")
    r = _git_run("merge-tree", "--write-tree", "--name-only", "HEAD", rec["branch"])
    if r.returncode == 1:
        conflict = True
        # 输出：第一行树 id，接着冲突文件名，空行之后是说明文字
        lines = r.stdout.decode().splitlines()[1:]
        files = lines[:lines.index("")] if "" in lines else lines
        out.append("跟主目录的代码打架（main 动过）"
                   + (f"：{'、'.join(files[:5])}" if files else ""))
    elif r.returncode != 0:
        out.append(f"预演合并失败：{r.stderr.decode(errors='replace').strip()[-200:]}")
    overlap = sorted(_main_dirty() & {f["path"] for f in _branch_files(rec["branch"])})
    if overlap:
        out.append(f"主目录里这几个文件有没提交的改动，PR 也改了它们：{'、'.join(overlap)}")
    return out, conflict


def merge_blockers(pr_id: int) -> list[str]:
    """只读预演：现在能不能合，合不了的原因（详情页一打开就显示）。"""
    return _blockers(get(pr_id))[0]


def merge(pr_id: int, force: bool = False) -> dict:
    """机主按「合并」：在主目录 git merge --no-ff，只在本地，永不 push。
    合完不删分支——等第 4 步重启验活，起不来要退回给他修，分支还得在。"""
    with _flock("_lock"):
        rec = get(pr_id)
        blockers, conflict = _blockers(rec)
        if blockers:
            if conflict:
                _event(rec["char"], f"#{pr_id}「{rec['title']}」合不进去：main 动过，跟你的改动打架了。"
                                    "pr_start 同一个名字重放一下，再 pr_open 更新。")
            raise PRError("；".join(blockers))
        tests = rec.get("tests") or {}
        if not tests.get("ok") and not force:
            raise NeedsForce(tests.get("summary") or "这个 PR 没有测试结果")
        msg = f"合并 PR #{pr_id}：{rec['title']}（{rec['char']}）"
        r = _git_run("merge", "--no-ff", "-q", "-m", msg, rec["branch"])
        if r.returncode:
            _git_run("merge", "--abort")
            err = (r.stderr or r.stdout).decode(errors="replace").strip()
            raise PRError(f"合并失败，已撤回：{err[-300:]}")
        now = _now()
        rec.update(status="merged", merged_at=now, updated_at=now, forced=bool(force),
                   merge_commit=_git("rev-parse", "HEAD").strip())
        _save(rec)
        _event(rec["char"], f"#{pr_id}「{rec['title']}」已合并。")
        return {"id": pr_id, "status": "merged", "merge_commit": rec["merge_commit"]}


def close_by_owner(pr_id: int, reason: str = "") -> dict:
    """机主关掉一个 PR（谁的都能关）。"""
    import config
    reason = (reason or "").strip()
    with _flock("_lock"):
        rec = get(pr_id)
        if rec["status"] not in UNFINISHED:
            raise PRError(f"#{pr_id} 已经{'合并' if rec['status'] == 'merged' else '关闭'}了")
        now = _now()
        rec.update(status="closed", closed_at=now, closed_by="owner",
                   close_reason=reason, updated_at=now)
        _save(rec)
        _event(rec["char"], f"#{pr_id}「{rec['title']}」被{config.user_name()}关了"
                            + (f"：{reason}" if reason else "。"))
        return {"id": pr_id, "status": "closed"}


def list_prs() -> list[dict]:
    """全部 PR：没办完的在前、收口的在后，各自按时间倒序（§5）。
    effective：合并了的此刻生没生效（None=不是合并态或不知道）。"""
    recs = sorted(_all_records(), key=lambda r: r.get("updated_at") or "", reverse=True)
    recs.sort(key=lambda r: _STATUS_ORDER.get(r["status"], 1))
    return [{k: r.get(k) for k in ("id", "char", "title", "status", "updated_at",
                                   "touches_ios")}
            | {"tests_ok": (r.get("tests") or {}).get("ok"), "effective": effective(r)}
            for r in recs]


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
    return rec | {"diff": diff, "effective": effective(rec)}


# ---------- 重启前后（§6.2；看守本体在 restart.py / tools/restart_guard.sh）----------

def pending_merges() -> list[tuple[int, str]]:
    """合并了、还没在跑着的代码里生效过的 PR：[(id, merge_commit)]，按合并先后。
    重启起不来时看守把它们一起退回（分不出是谁弄坏的）。只算还在主目录 HEAD 里的合并。"""
    out = []
    for r in sorted(_all_records(), key=lambda r: (r.get("merged_at") or "", r["id"])):
        if effective(r) is False and \
                _git_run("merge-base", "--is-ancestor", r["merge_commit"], "HEAD").returncode == 0:
            out.append((r["id"], r["merge_commit"]))
    return out


def settle_rollback() -> list[int]:
    """认领看守留下的退回标记（新后端启动时调，在 record_live_head 之前）：这些 PR 标「已退回」、
    写 revert_reason（起不来时的日志尾巴）、通知提的角色去修。回认领了哪些 PR。"""
    marker = PRS_DIR / "_rollback"
    if not marker.exists():
        return []
    try:
        tail = (PRS_DIR / "_rollback_log").read_text().strip()
    except FileNotFoundError:
        tail = ""
    rows = []
    for ln in marker.read_text().splitlines():
        parts = ln.split()
        if len(parts) == 3 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[2]))
    batch = "、".join(f"#{i}" for i, _ in sorted(rows))
    done = []
    with _flock("_lock"):
        for pr_id, revert_sha in rows:
            try:
                rec = get(pr_id)
            except PRError:
                continue
            if rec["status"] != "merged":
                continue
            now = _now()
            others = len(rows) > 1
            reason = ("合进主目录后重启，后端起不来，看守脚本自动退回了"
                      + (f"（同一批一共退了 {batch}，分不出是谁的问题，不一定是你这个）" if others else "")
                      + "。起不来时的日志：\n" + (tail or "（没截到）"))
            rec.update(status="reverted", reverted_at=now, updated_at=now,
                       revert_commit=revert_sha, revert_reason=reason)
            _save(rec)
            _event(rec["char"], f"#{pr_id}「{rec['title']}」合进去之后后端起不来，已被自动退回"
                                + (f"（同一批还退了别的：{batch}，不一定是你这个的问题）" if others else "")
                                + "。pr_view 看起不来时的日志；要修就 pr_start 同一个名字（会在最新 main 上"
                                  "重建你的改动），修好 pr_open；不修就 pr_close。")
            done.append(pr_id)
    marker.unlink(missing_ok=True)
    (PRS_DIR / "_rollback_log").unlink(missing_ok=True)
    return done


CLOSED_BRANCH_KEEP_DAYS = 7


def cleanup_branches() -> list[str]:
    """清分支（§10.4）：合并了且已经在跑着的代码里生效（=重启验过活）的，删；关掉 7 天的，删。
    合了但还没生效的不删——重启起不来要退回给他修，分支还得在。记录永远留着。
    分支正被工作副本检出着删不掉，跳过，下次再说。"""
    gone = []
    now = datetime.now().astimezone()
    for r in _all_records():
        if not _branch_exists(r["branch"]):
            continue
        if r["status"] == "merged":
            drop = effective(r) is True
        elif r["status"] == "closed":
            try:
                at = datetime.fromisoformat(r.get("closed_at") or r["updated_at"])
            except (KeyError, ValueError):
                continue
            drop = (now - at).days >= CLOSED_BRANCH_KEEP_DAYS
        else:
            drop = False
        if drop and _git_run("branch", "-D", r["branch"]).returncode == 0:
            gone.append(r["branch"])
    return gone
