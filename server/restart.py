"""app 上的「重启后端」按钮（PLAN_prs §6.2）：预检 → 起脱离的看守脚本 → 回 202 等着被它杀。

真正的 kill / 起 / 验活 / 起不来自动退回，全在 tools/restart_guard.sh 里一次做完——
后端一停 app 就连不上，要人按的东西一样都不能留在这之后（restart-backend-one-command）。
这儿只做看守起飞之前能做的：挡掉起了必死的（语法 / import 错），交给看守「起不来退哪几个合并」。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import config
import prs

SERVER_DIR = Path(__file__).resolve().parent
GUARD = SERVER_DIR / "tools" / "restart_guard.sh"
UVICORN = SERVER_DIR / ".venv" / "bin" / "uvicorn"
LOG = SERVER_DIR / "nohup.out"
PREFLIGHT_TIMEOUT_SEC = 120
LOCK_STALE_SEC = 300      # 看守最长也就 15+10+60+60 秒左右，锁比这老 = 看守死了没收


class RestartError(RuntimeError):
    """不重启的原因，给机主看的原话。"""


# 预检：主目录的代码用主目录的 venv 能不能 import 起来。按函数懒 import 的模块 import app
# 碰不到，所以先把 server/ 下每个 .py 编译一遍抓语法错。只抓「起了必死」的那类——
# 启动过程里才炸的（lifespan）要靠看守的验活兜。
_PREFLIGHT = """
import pathlib
for p in sorted(pathlib.Path('.').glob('*.py')):
    compile(p.read_bytes(), str(p), 'exec')
import app
"""


def preflight() -> None:
    try:
        r = subprocess.run([str(prs.PYTHON), "-c", _PREFLIGHT], cwd=SERVER_DIR,
                           env=config.BOOT_ENV, capture_output=True, text=True,
                           timeout=PREFLIGHT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        raise RestartError(f"预检 {PREFLIGHT_TIMEOUT_SEC}s 没跑完（import app 卡住了），没重启")
    if r.returncode:
        tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-12:])
        raise RestartError("新代码 import 不起来，没重启（后端还是旧的在跑）：\n" + tail)


def _lock_path() -> Path:
    return prs.PRS_DIR / "_restarting"


def in_progress() -> bool:
    p = _lock_path()
    try:
        return time.time() - p.stat().st_mtime < LOCK_STALE_SEC
    except FileNotFoundError:
        return False


def blockers() -> list[str]:
    """按下之前就能说的「不能重启」（不含预检，预检要跑几秒，按了才跑）。"""
    out = []
    if in_progress():
        out.append("已经在重启了，等一下")
    if prs.pending_merges():
        cur = prs._git_run("symbolic-ref", "--short", "-q", "HEAD").stdout.decode().strip()
        if cur != prs.MAIN:
            out.append(f"主目录现在在 {cur or '游离的提交'} 上，不在 {prs.MAIN}——"
                       "起不来时没法自动退回合并的 PR，切回去再重启")
    return out


def spawn(port: int, old_pid: int) -> dict:
    """预检过了就起看守（脱离：自己一个 session，后端死了它还在），回交给它的退回清单。"""
    b = blockers()
    if b:
        raise RestartError("；".join(b))
    preflight()
    reverts = prs.pending_merges()
    prs.PRS_DIR.mkdir(parents=True, exist_ok=True)
    _lock_path().write_text(str(time.time()))
    env = dict(config.BOOT_ENV,
               GUARD_REPO=str(prs.REPO_ROOT), GUARD_SERVER_DIR=str(SERVER_DIR),
               GUARD_UVICORN=str(UVICORN), GUARD_HOST=os.environ.get("CASSETTE_HOST", "0.0.0.0"),
               GUARD_PORT=str(port), GUARD_OLD_PID=str(old_pid),
               GUARD_REVERTS=" ".join(f"{i}:{sha}" for i, sha in reverts),
               GUARD_STATE=str(prs.PRS_DIR), GUARD_LOG=str(LOG),
               GUARD_BARK=config.BARK_URL)
    try:
        subprocess.Popen(["/bin/bash", str(GUARD)], cwd=SERVER_DIR, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError as e:
        _lock_path().unlink(missing_ok=True)
        raise RestartError(f"看守脚本起不来，没重启：{e}")
    return {"reverts_if_fail": [i for i, _ in reverts]}
