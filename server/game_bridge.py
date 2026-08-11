"""游戏桥：MuMu 模拟器 + MaaFramework 任务引擎的宿主侧真身（game-task / game-story 两个
插件共用的底座）。

分工（见 README 的 Game mode 一节）：
- **任务**（game-task 插件）：日常清体力交给声明式 pipeline 确定性执行——引擎照
  MaaYuan 的任务图自己截屏、模板匹配、点按，AI 只负责挑任务、看结果、汇报。
- **剧情**（game-story 插件）：AI 本人盲操读剧情，走 code_bridge 的常驻会话（另一条路）。
- 这里管两边共用的四样：设备自愈、急停锁、互斥锁、两本笔记本；外加任务引擎 runner。

资源与依赖（默认不装，机主显式开通）：
- 任务图来自 MaaYuan（https://github.com/syoius/MaaYuan ，MIT）——就是给《如鸢》写的，
  钉 commit 使用；游戏改版后由机主跑 tools/fetch_maayuan.py 升钉，同插件 registry 纪律。
- 引擎依赖见 requirements-game.txt。⚠️ maafw 必须钉 ==5.0.5：新版是 pipeline v5 语法，
  直接拒绝解析 MaaYuan 的资源（实测 5.12 报 parse_task failed）。
- maa 的 import 全部懒加载：没装依赖/没开 GAME_MODE 时后端照常起，工具路有声报错。

几处非搬不可的细节（实测踩出来的）：
- mumutool 改配置的键是 resolutionWidthHeight/resolutionDPI（vm.json 里的 framebuffer*
  是结果不是入口）。模拟器必须跑在 720x1280@320——MaaYuan 资源的原生设计分辨率。
- MuMuPlayer 进程活着才有 server port；设备 adb 端口是动态的，每次从 mumutool info 拿，
  别写死 16384。
- MaaYuan 的 agent/main.py 启动时会**自己 pip 装 requirements**（走镜像源装进当前
  venv）。依赖已在 requirements-game.txt 里预装齐，它检查一遍装不动新东西，几秒过。
- 引擎跑一半按急停：pipeline 节点粒度太细，中途硬断会把游戏留在奇怪的界面上。所以
  急停对 runner 的语义是「跑完当前任务就收手」，真要立刻断用 stop()（post_stop）。
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import config
import state_store
from notify import bark_push, logerr

BASE = Path(__file__).resolve().parent
GAME_DIR = BASE / "state" / "game"
MAAYUAN_DIR = GAME_DIR / "maayuan"                    # tools/fetch_maayuan.py 钉 commit 放这儿
RES_DIR = MAAYUAN_DIR / "assets" / "resource" / "base"
INTERFACE_PATH = MAAYUAN_DIR / "assets" / "interface.json"
AGENT_MAIN = MAAYUAN_DIR / "agent" / "main.py"
TASK_LOG_PATH = GAME_DIR / "task_log.jsonl"
LOCK_PATH = GAME_DIR / "lock.json"

MUMU_APP = "/Applications/MuMuPlayer.app"
MUMUTOOL = MUMU_APP + "/Contents/MacOS/mumutool"
ADB = MUMU_APP + "/Contents/MacOS/MuMuEmulator.app/Contents/MacOS/tools/adb"
VM_INDEX = os.environ.get("GAME_VM_INDEX", "0")
BOOT_WAIT_SEC = 90        # 冷启动模拟器的等待上限

# 游戏笔记本（一本）：剧情脉络/机主的话/AI 自己攒的坐标修正，全在这儿。
# 最初设计拆过任务本/剧情本两本，08-12 收敛回一本：任务侧的「选项偏好」被任务集
# （presets）接走之后，任务本就没剩什么可记的了。
# 出厂空白——固定知识在插件的出厂纪律文件里，笔记本只装 AI 自己攒的增量。
# 键上保留 task/story 两个旧别名指向同一本：插件工具和 app 的老路径不用一起换血。
NOTES_PATH = GAME_DIR / "notes.md"
NOTES_PATHS = {"game": NOTES_PATH, "task": NOTES_PATH, "story": NOTES_PATH}
NOTES_MAX_CHARS = 50_000


class GameModeOff(Exception):
    """game 模式没在 .env 里打开。路由层转成 503。"""


def require_enabled() -> None:
    if not config.GAME_MODE_ENABLED:
        raise GameModeOff("game 模式没开：在 server/.env 里设 GAME_MODE_ENABLED=1 再重启后端")


# ---------- 急停锁（app 顶栏 ⏸ 按钮）----------
# ⚠️ 单独一个文件，**别挪进 settings.json**：POST /settings 是 app 按字段白名单整体
# 覆盖重写的（不 merge），机主在设置页随便动一个选项就会把塞进去的 game_paused 静默
# 抹掉——急停自己解除是安全事故，不是小毛病。
PAUSED_PATH = GAME_DIR / "paused.json"


def paused() -> bool:
    # 每次从盘读：机主按急停的是 app 那条请求，这边的 runner 线程要即时看见。
    return bool(state_store._read_json(PAUSED_PATH, {}).get("paused"))


def set_paused(value: bool) -> None:
    GAME_DIR.mkdir(parents=True, exist_ok=True)
    state_store._write_json(PAUSED_PATH, {"paused": bool(value), "ts": int(time.time())})


# ---------- 互斥锁（模拟器只有一台：任务引擎和剧情会话不能同时上手）----------
_LOCK_GUARD = threading.Lock()   # 进程内的读改写锁；跨进程场景（剧情会话）靠文件本身


def lock_owner() -> Optional[str]:
    d = state_store._read_json(LOCK_PATH, {})
    return d.get("owner") or None


def acquire_lock(owner: str) -> Optional[str]:
    """拿模拟器使用权。成功返回 None，被占返回持有者名（转述给 AI 用）。"""
    with _LOCK_GUARD:
        cur = lock_owner()
        if cur and cur != owner:
            return cur
        GAME_DIR.mkdir(parents=True, exist_ok=True)
        state_store._write_json(LOCK_PATH, {"owner": owner, "since": int(time.time())})
        return None


def release_lock(owner: str) -> None:
    # 只释放自己拿的锁：别把对方正用着的锁顺手清了。
    with _LOCK_GUARD:
        if lock_owner() == owner and LOCK_PATH.exists():
            LOCK_PATH.unlink()


# ---------- 两本笔记本 ----------
def read_notes(book: str) -> str:
    path = NOTES_PATHS[book]
    return path.read_text("utf-8") if path.exists() else ""


def write_notes(book: str, content: str) -> Optional[str]:
    """整本替换。超长返回错误说明（不截断——截了 AI 也不知道丢了哪段）。"""
    if len(content) > NOTES_MAX_CHARS:
        return f"太长了（{len(content)} 字符 > {NOTES_MAX_CHARS}），精简后再写"
    path = NOTES_PATHS[book]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, "utf-8")
    tmp.replace(path)
    return None


def notes_status() -> dict:
    if NOTES_PATH.exists():
        return {"chars": len(NOTES_PATH.read_text("utf-8")),
                "updated_at": int(NOTES_PATH.stat().st_mtime)}
    return {"chars": 0, "updated_at": None}


# ---------- 任务集（presets：机主在游戏页存的「一串任务+定制选项」）----------
# 存宿主不存手机：AI 也要查得到——机主在聊天里说「做日常任务集」，AI 用 task_run_preset
# 照单派活。列表新的在前，「默认选最上面」= 默认选最新设定的。
PRESETS_PATH = GAME_DIR / "presets.json"


def read_presets() -> list[dict]:
    return state_store._read_json(PRESETS_PATH, [])


def get_preset(name: str) -> Optional[dict]:
    return next((p for p in read_presets() if p.get("name") == name), None)


def save_preset(name: str, names: list[str], options: dict) -> None:
    """新建或覆盖（同名整个替换），并挪到最前。覆盖前的确认在 app 侧做。"""
    GAME_DIR.mkdir(parents=True, exist_ok=True)
    presets = [p for p in read_presets() if p.get("name") != name]
    presets.insert(0, {"name": name, "names": names, "options": options,
                       "ts": int(time.time())})
    state_store._write_json(PRESETS_PATH, presets)


def delete_preset(name: str) -> bool:
    presets = read_presets()
    kept = [p for p in presets if p.get("name") != name]
    if len(kept) == len(presets):
        return False
    state_store._write_json(PRESETS_PATH, kept)
    return True


# ---------- 设备自愈（MuMu 开机 + adb 连上；只返回 error 字符串，绝不抛异常）----------
_adb_serial: Optional[str] = None    # 进程内缓存；连接断了下次 ensure 会重连


def _mumutool_json(*args: str) -> dict:
    try:
        p = subprocess.run([MUMUTOOL, *args], capture_output=True, timeout=30)
        try:
            return json.loads(p.stdout.decode("utf-8", "replace"))
        except Exception:
            return {"error": p.stderr.decode("utf-8", "replace").strip()
                    or f"mumutool {args[0]} 无输出"}
    except Exception as e:
        return {"error": f"mumutool {args[0]} 失败: {e}"}


def ensure_device() -> tuple[Optional[str], Optional[str]]:
    """保证模拟器开机 + adb 已连。返回 (serial, None) 或 (None, 错误说明)。"""
    global _adb_serial
    if _adb_serial:
        p = subprocess.run([ADB, "-s", _adb_serial, "shell", "echo", "ok"],
                           capture_output=True, timeout=10)
        if p.returncode == 0:
            return _adb_serial, None
        _adb_serial = None
    if not os.path.isdir(MUMU_APP):
        return None, "MuMu 模拟器没装（找不到 MuMuPlayer.app）"
    # 1) MuMuPlayer 进程活着才有 server port
    if "server-port" not in _mumutool_json("port"):
        subprocess.run(["open", "-a", "MuMuPlayer"], capture_output=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            if "server-port" in _mumutool_json("port"):
                break
            time.sleep(2)
        else:
            return None, "MuMuPlayer 启动不了，让机主看一眼"
    # 2) 设备开机（stopped → open，轮询到 running 且拿到动态 adb_port）
    port = None
    deadline = time.time() + BOOT_WAIT_SEC
    while time.time() < deadline:
        info = _mumutool_json("info", VM_INDEX).get("return", {})
        if info.get("state") == "running" and info.get("adb_port"):
            port = info["adb_port"]
            break
        if info.get("state") == "stopped":
            _mumutool_json("open", VM_INDEX)
        time.sleep(3)
    if not port:
        return None, f"模拟器 {BOOT_WAIT_SEC}s 内没能开机，让机主看一眼"
    # 3) adb connect + 冒烟
    serial = f"127.0.0.1:{port}"
    try:
        subprocess.run([ADB, "connect", serial], capture_output=True, timeout=15)
        p = subprocess.run([ADB, "-s", serial, "shell", "echo", "ok"],
                           capture_output=True, timeout=10)
        if p.returncode != 0:
            return None, f"adb 连上了但 shell 不通: {p.stderr.decode('utf-8', 'replace')[:200]}"
    except Exception as e:
        return None, f"adb 连接失败: {e}"
    _adb_serial = serial
    return serial, None


# ---------- MaaYuan interface.json 解析（任务目录 + 选项 → pipeline_override）----------
def resource_ready() -> bool:
    return INTERFACE_PATH.exists() and (RES_DIR / "pipeline").is_dir()


def _interface() -> dict:
    return json.loads(INTERFACE_PATH.read_text("utf-8"))


def tasks_catalog() -> list[dict]:
    """AI 和 app 看到的同一份任务菜单。advanced（自由输入型参数）一期不支持，标出来。"""
    data = _interface()
    options = data.get("option", {})
    out = []
    for t in data.get("task", []):
        opts = []
        for opt_name in t.get("option", []):
            opt = options.get(opt_name) or {}
            opts.append({
                "name": opt_name,
                "cases": [c.get("name", "") for c in opt.get("cases", [])],
                "default": opt.get("default_case"),
            })
        out.append({
            "name": t.get("name", ""),
            "entry": t.get("entry", ""),
            "doc": t.get("doc", ""),
            "options": opts,
            "unsupported_advanced": t.get("advanced", []),
            # =====xxx===== 是 MaaYuan 给自家 GUI 用的视觉分组条目（entry 恒为 stop）：
            # 标出来让 app 渲染成分组头，别当成可跑的任务。
            "separator": _is_separator(t),
        })
    return out


def _is_separator(task: dict) -> bool:
    n = (task.get("name") or "").strip()
    return len(n) > 2 and n.startswith("=") and n.endswith("=")


def _merge_override(dst: dict, src: dict) -> None:
    # 节点级浅合并：同一节点被任务本体和选项都摸过时，字段合并而不是整个盖掉。
    for node, patch in (src or {}).items():
        if isinstance(patch, dict) and isinstance(dst.get(node), dict):
            dst[node].update(patch)
        else:
            dst[node] = patch


def resolve_task(name: str, choices: dict[str, str]) -> tuple[str, dict]:
    """任务名 + 选项选择 → (entry, 合并后的 pipeline_override)。
    选项没给的用 default_case；连 default 都没有的选第一个 case（并在日志里说清）。"""
    data = _interface()
    task = next((t for t in data.get("task", []) if t.get("name") == name), None)
    if task is None:
        raise KeyError(f"任务不存在: {name}")
    if _is_separator(task):
        raise KeyError(f"「{name}」是分组标题不是任务，别派它")
    override: dict = {}
    _merge_override(override, task.get("pipeline_override", {}))
    options = data.get("option", {})
    for opt_name in task.get("option", []):
        opt = options.get(opt_name) or {}
        cases = opt.get("cases", [])
        chosen = choices.get(opt_name) or opt.get("default_case") \
            or (cases[0].get("name") if cases else None)
        case = next((c for c in cases if c.get("name") == chosen), None)
        if case:
            _merge_override(override, case.get("pipeline_override", {}))
    return task.get("entry", ""), override


# ---------- 任务引擎 runner（后台线程，一次一串任务）----------
# 状态只在内存 + task_log.jsonl：runner 跟着后端进程活，后端重启 = 跑动中的任务作废
# （引擎断了游戏自己停在原地，无害），日志里留着断点，AI 下次看 status 就知道。
_runner_lock = threading.Lock()
_runner: dict = {"running": False, "queue": [], "current": None,
                 "started_at": None, "stop_requested": False, "run_id": None}
_tasker_ref: dict = {"tasker": None}   # stop() 要够得着正在跑的 tasker


def _append_log(entry: dict) -> None:
    GAME_DIR.mkdir(parents=True, exist_ok=True)
    entry["ts"] = int(time.time())
    with TASK_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(limit: int = 50) -> list[dict]:
    if not TASK_LOG_PATH.exists():
        return []
    out = []
    for ln in TASK_LOG_PATH.read_text("utf-8").splitlines():
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out[-limit:]


def status() -> dict:
    with _runner_lock:
        return {
            "enabled": config.GAME_MODE_ENABLED,
            "resource_ready": resource_ready(),
            "paused": paused(),
            "lock_owner": lock_owner(),
            "running": _runner["running"],
            "current": _runner["current"],
            "queue": list(_runner["queue"]),
            "started_at": _runner["started_at"],
            "run_id": _runner["run_id"],
            "recent": read_log(10),
        }


def start_tasks(names: list[str], options: dict[str, dict[str, str]]) -> dict:
    """校验并起后台线程跑一串任务。返回 {ok} 或 {error}。options 按任务名给：
    {"任务名": {"选项名": "case 名"}}。"""
    require_enabled()
    if not resource_ready():
        return {"error": "任务资源没就绪：让机主跑一遍 server/tools/fetch_maayuan.py"}
    if paused():
        return {"error": "⏸ 机主按了游戏急停：现在不能开新任务，先问机主"}
    resolved = []
    try:
        for n in names:
            entry, override = resolve_task(n, options.get(n, {}))
            resolved.append({"name": n, "entry": entry, "override": override})
    except KeyError as e:
        return {"error": f"{e.args[0]}（用 task_list 查正确的任务名）"}
    if not resolved:
        return {"error": "任务列表是空的"}
    with _runner_lock:
        if _runner["running"]:
            return {"error": f"引擎正在跑「{_runner['current']}」，等它完或先 task_stop"}
        holder = acquire_lock("task")
        if holder:
            return {"error": f"模拟器正被{'剧情会话' if holder == 'story' else holder}占着，"
                             "两边不能同时上手"}
        run_id = uuid.uuid4().hex[:8]
        _runner.update(running=True, queue=[r["name"] for r in resolved],
                       current=None, started_at=int(time.time()),
                       stop_requested=False, run_id=run_id)
    threading.Thread(target=_run_thread, args=(resolved, run_id),
                     daemon=True, name="game-task-runner").start()
    return {"ok": True, "run_id": run_id, "queue": [r["name"] for r in resolved]}


def stop_tasks() -> dict:
    """立刻中止：置停止位 + post_stop 打断引擎当前节点。游戏可能停在半路界面，无害。"""
    with _runner_lock:
        if not _runner["running"]:
            return {"ok": True, "note": "引擎本来就没在跑"}
        _runner["stop_requested"] = True
    t = _tasker_ref["tasker"]
    if t is not None:
        try:
            t.post_stop()
        except Exception as e:
            logerr(f"game runner post_stop 失败: {e}")
    return {"ok": True}


def _run_thread(resolved: list[dict], run_id: str) -> None:
    done, failed = [], []
    agent_proc = None
    try:
        serial, err = ensure_device()
        if err:
            _append_log({"run_id": run_id, "task": "(设备)", "status": "error", "detail": err})
            failed.append(f"设备: {err}")
            return
        # maa 懒加载：装没装依赖只影响这条路，不拖垮后端
        try:
            from maa.resource import Resource
            from maa.controller import AdbController
            from maa.tasker import Tasker
            from maa.agent_client import AgentClient
        except ImportError as e:
            msg = f"maafw 没装（pip install -r requirements-game.txt）: {e}"
            _append_log({"run_id": run_id, "task": "(引擎)", "status": "error", "detail": msg})
            failed.append(msg)
            return
        res = Resource()
        res.post_bundle(str(RES_DIR)).wait()
        if not res.loaded:
            _append_log({"run_id": run_id, "task": "(引擎)", "status": "error",
                         "detail": "资源加载失败（看后端 stderr 的 MaaFW 日志）"})
            failed.append("资源加载失败")
            return
        ctrl = AdbController(adb_path=ADB, address=serial)
        ctrl.post_connection().wait()
        if not ctrl.connected:
            _append_log({"run_id": run_id, "task": "(引擎)", "status": "error",
                         "detail": "控制器连不上模拟器"})
            failed.append("控制器连不上")
            return
        tasker = Tasker()
        tasker.bind(res, ctrl)
        _tasker_ref["tasker"] = tasker
        # agent：MaaYuan 的自定义识别/动作（派遣、鸟报等任务依赖）。起不来不拦整串任务——
        # 纯模板任务照跑，用到自定义节点的那个任务自己失败并留日志。
        try:
            client = AgentClient()
            client.bind(res)
            agent_proc = subprocess.Popen(
                [sys.executable, str(AGENT_MAIN), "-u", client.identifier],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            client.set_timeout(60_000)
            if not client.connect():
                logerr("game runner: MaaYuan agent 连不上，自定义节点的任务会失败")
        except Exception as e:
            logerr(f"game runner: agent 启动失败（{e}），继续跑纯模板任务")
        for i, item in enumerate(resolved):
            with _runner_lock:
                if _runner["stop_requested"]:
                    break
                _runner["current"] = item["name"]
                _runner["queue"] = [x["name"] for x in resolved[i + 1:]]
            if paused():
                # 急停语义：跑完当前任务就收手 → 到这儿是任务间隙，直接收
                _append_log({"run_id": run_id, "task": item["name"], "status": "skipped",
                             "detail": "急停中，剩余任务没跑"})
                break
            t0 = time.time()
            try:
                detail = tasker.post_task(item["entry"], item["override"]).wait().get()
                ok = detail is not None and getattr(detail.status, "succeeded", False)
                _append_log({"run_id": run_id, "task": item["name"],
                             "status": "done" if ok else "failed",
                             "seconds": int(time.time() - t0)})
                (done if ok else failed).append(item["name"])
            except Exception as e:
                _append_log({"run_id": run_id, "task": item["name"], "status": "error",
                             "detail": str(e)[:300], "seconds": int(time.time() - t0)})
                failed.append(item["name"])
    finally:
        _tasker_ref["tasker"] = None
        if agent_proc is not None:
            agent_proc.terminate()
        release_lock("task")
        with _runner_lock:
            _runner.update(running=False, current=None, queue=[], stop_requested=False)
        summary = f"游戏任务跑完：成 {len(done)}" + (f"，败 {len(failed)}" if failed else "")
        _append_log({"run_id": run_id, "task": "(收尾)", "status": "summary",
                     "detail": summary, "done": done, "failed": failed})
        bark_push(summary)
