"""运行时状态存储（纯文件 I/O，不依赖 app，可独立测试）。
所有状态放 server/state/（已 gitignore）。写入走临时文件 + 原子替换，避免半截文件。

多角色（PLAN_multichar M1）：带角色身份的状态按 state/characters/<char_id>/ 分目录——
wake_log / recent_window / schedule / browse_log / settings(角色部分) / persona_rendered。
所有相关函数加了缺省 char_id=None（→ 默认角色 "default"），老调用点一行不改语义不变。
outbox / sticker_catalog 保持全局一份（outbox 条目自带 char_id 字段由写入方填；
贴纸库一期各角色共用）。旧的扁平布局在 import 时一次性迁入默认角色目录（幂等）。"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

from typing import Optional

STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_DIR.mkdir(exist_ok=True)

DEFAULT_CHAR_ID = "default"
CHAR_STATE_ROOT = STATE_DIR / "characters"

OUTBOX_PATH = STATE_DIR / "outbox.json"
SETTINGS_PATH = STATE_DIR / "settings.json"   # 全局设置：只剩用户自己的（user_name/pronoun）

RECENT_WINDOW_N = 300  # 窗口存储容量上限；wake 实际注入条数由 settings.wake_window_n 决定


def char_state_dir(char_id: Optional[str] = None) -> Path:
    """角色的 state 目录（懒建）。这里不校验角色是否注册——注册表是 characters.py 的事，
    state 层保持纯文件 I/O；上层（API）先 resolve 再进来。"""
    d = CHAR_STATE_ROOT / (char_id or DEFAULT_CHAR_ID)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _char_path(name: str, char_id: Optional[str] = None) -> Path:
    return char_state_dir(char_id) / name


def _migrate_legacy_layout() -> None:
    """旧扁平布局 → 默认角色目录，import 时跑一次（幂等）。

    必须能处理「新旧代码跨窗口并跑」：老进程还活着时新代码先迁了一次，老进程随后又在
    旧路径新建文件继续写——单纯「目标存在就跳过」会把那批写入永远丢在旧位。所以：
    - append-only 的 .jsonl：两头都在 → 旧文件内容**追加合并**进新位，再删旧文件；
    - 快照类 .json：两头都在 → 谁 mtime 新用谁（老进程后写的快照是更新的世界）。
    settings.json 特殊：**拷不搬**——它同时装着用户键（user_name/pronoun，留在全局）
    和角色键（wake 策略等，进角色目录）；load/save 各取各的，残留的对方键被忽略。
    同样按 mtime：旧全局文件比角色文件新（老进程存过设置）就重拷一次。"""
    d = CHAR_STATE_ROOT / DEFAULT_CHAR_ID
    moves = ["wake_log.jsonl", "recent_window.json", "schedule.json", "browse_log.jsonl",
             "plugins_enabled.json", "plugins_wake_enabled.json"]
    if any((STATE_DIR / n).exists() for n in moves):
        d.mkdir(parents=True, exist_ok=True)
        for n in moves:
            src, dst = STATE_DIR / n, d / n
            if not src.exists():
                continue
            if not dst.exists():
                os.replace(src, dst)
            elif n.endswith(".jsonl"):
                with dst.open("a", encoding="utf-8") as f:
                    f.write(src.read_text("utf-8"))
                src.unlink()
            elif src.stat().st_mtime > dst.stat().st_mtime:
                os.replace(src, dst)
            else:
                src.unlink()
    if SETTINGS_PATH.exists():
        cs = d / "settings.json"
        if not cs.exists() or SETTINGS_PATH.stat().st_mtime > cs.stat().st_mtime:
            d.mkdir(parents=True, exist_ok=True)
            cs.write_text(SETTINGS_PATH.read_text("utf-8"), "utf-8")


_migrate_legacy_layout()

# settings 里属于"用户本人"的键（全局唯一，不随角色走）；其余全是角色键。
USER_SETTINGS_KEYS = {"user_name", "user_pronoun"}

# settings 内置默认（app 没同步过也能跑）
DEFAULT_SETTINGS = {
    "agent_name": "",            # AI 的名字；空 = 用 config 的兜底默认
    "user_name": "",             # 用户昵称；空 = 用 config 的兜底默认
    "enabled": True,
    "active_start": "10:00",
    "active_end": "24:00",
    "day_freq": "mid",           # low | mid | high
    "night_freq": "low",         # low | mid | high
    "daily_max": 10,             # null = 不限每天条数
    "min_interval_min": 60,      # null = 关闭最小间隔
    "quiet_after_user_min": 20,  # null = 关闭"刚聊过就别戳"
    "wake_window_n": 50,         # wake 注入最近窗口条数（夹 20~300）
    "wake_daily_budget": None,   # 每天最多自发醒来次数；null = 不限。拦的是"醒"本身（省 token），
                                 # 与 daily_max（只拦推送）不同；硬触发（提醒/邮件）豁免但计数
    "user_pronoun": "TA",        # 提到用户时的人称代词：她 | 他 | TA（用户在设置里选）
}


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    # 临时文件名带唯一后缀（pid+uuid）：固定 .tmp 名并发写同一文件会互相抢，实测撞车过。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)   # 原子替换


# 多写入方的读改写锁（原子替换只防半截文件，防不了并发读改写互相覆盖）：
# recent_window / schedule 的写入方有 chat 事件循环和 wake 执行线程两个。
WINDOW_LOCK = threading.Lock()
SCHEDULE_LOCK = threading.Lock()


# ---------- 本轮在飞的正文（只在内存里，绝不落盘）----------
# 自切 code 模式（/codemode/start）是 TA 在 `claude -p` **跑到一半**调工具触发的，而这一轮
# 的回复要等子进程结束、finalize_chat_reply 才写进 recent_window——所以那一刻注入过去的
# 对话断在用户那条上，TA 自己刚说过的话看不见。
# 实机事故（08-10 14:36）：聊天里刚定了邮箱前缀 cassette.woof，切过去的 context.md 里
# 一个 woof 都没有，code 那边又自己想了个 cassette.hears，两边前缀对不上。
# 那段话其实一直在服务端手里（sse.translate_events 的正文段），只是活在别的请求的栈上，
# /codemode/start 够不着 → 在这儿开个进程内的中转。
# 读写都是整根字符串赋值，GIL 下是原子的，不用锁；跨进程不共享也不需要——写的（SSE 事件
# 循环）和读的（/codemode/start）本来就在同一个后端进程里。
_live_reply = {"text": ""}


def set_live_reply(text: str) -> None:
    _live_reply["text"] = text or ""


def get_live_reply() -> str:
    return _live_reply["text"]


def clear_live_reply() -> None:
    """轮开始和收尾各清一次。**只在收尾清不够**：断连/异常那一支不走收尾，
    残留的上一轮正文会漏进下一次自切。"""
    _live_reply["text"] = ""


# ---------- settings ----------
# 一份"完整设置"逻辑上由两处拼成：全局文件出用户键（USER_SETTINGS_KEYS），
# 角色文件出其余键。load/save 对上层保持"一个 dict 进出"的旧接口。
def load_settings(char_id: Optional[str] = None) -> dict:
    s = dict(DEFAULT_SETTINGS)
    g = _read_json(SETTINGS_PATH, {})
    s.update({k: v for k, v in g.items() if k in USER_SETTINGS_KEYS})
    c = _read_json(_char_path("settings.json", char_id), {})
    s.update({k: v for k, v in c.items() if k not in USER_SETTINGS_KEYS})
    return s


def save_settings(s: dict, char_id: Optional[str] = None) -> None:
    g = _read_json(SETTINGS_PATH, {})
    g.update({k: v for k, v in s.items() if k in USER_SETTINGS_KEYS})
    _write_json(SETTINGS_PATH, g)
    _write_json(_char_path("settings.json", char_id),
                {k: v for k, v in s.items() if k not in USER_SETTINGS_KEYS})


# ---------- recent window（最近对话快照，per 角色）----------
# 醒来时 app 不在场，模型靠这份快照拿上下文（app 仍是历史的唯一主人，这只是后端侧的影子）。
def write_recent_window(messages: list[dict], char_id: Optional[str] = None) -> None:
    _write_json(_char_path("recent_window.json", char_id), messages[-RECENT_WINDOW_N:])


def read_recent_window(char_id: Optional[str] = None) -> list[dict]:
    return _read_json(_char_path("recent_window.json", char_id), [])


# ---------- wake log（醒来极简元数据，append-only；删除时整体重写；per 角色）----------
def append_wake_log(entry: dict, char_id: Optional[str] = None) -> None:
    with _char_path("wake_log.jsonl", char_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def overwrite_wake_log(entries: list[dict], char_id: Optional[str] = None) -> None:
    """整体重写 wake_log（心流日志删除某条后用）。唯一临时名 + 原子替换（mianmian 同款）。"""
    p = _char_path("wake_log.jsonl", char_id)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), "utf-8")
    tmp.replace(p)


def read_wake_log(limit: Optional[int] = None, char_id: Optional[str] = None) -> list[dict]:
    p = _char_path("wake_log.jsonl", char_id)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text("utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out[-limit:] if limit else out


# ---------- browse log（TA 浏览过哪些网页，append-only；per 角色）----------
# 聊天/醒来两条路的 navigate 都进这里。眼下只落盘不做读取端点：聊天里有聚合灰字可看，
# 这份留给以后的 Mind 页当素材（同 wake_log 的思路，别清理）。
def append_browse_log(entry: dict, char_id: Optional[str] = None) -> None:
    with _char_path("browse_log.jsonl", char_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------- 表情库清单（app 每次 /chat 存一份，醒来时后端不在 app 手里也能挑表情发）----------
STICKER_CATALOG_PATH = STATE_DIR / "sticker_catalog.json"


def read_sticker_catalog() -> list[dict]:
    return _read_json(STICKER_CATALOG_PATH, [])


def write_sticker_catalog(catalog: list[dict]) -> None:
    _write_json(STICKER_CATALOG_PATH, catalog)


# ---------- 醒来调度（next_wake_at 持久化，跨重启；per 角色）----------
# 调度器 tick 靠这份落盘：不落盘的话频繁重启会把定时器重置、永远轮不到自主醒来。
def read_schedule(char_id: Optional[str] = None) -> dict:
    return _read_json(_char_path("schedule.json", char_id), {})


def write_schedule(d: dict, char_id: Optional[str] = None) -> None:
    _write_json(_char_path("schedule.json", char_id), d)


# ---------- outbox（待送达盒子）----------
# 客户端断连时生成完的回复投进这里，app 回前台轮询 /pending 补上屏。
# 写入方不止一个（断连补投 / 以后的主动消息），读改写要锁——原子替换只防半截文件，
# 防不了并发追加互相覆盖。
_OUTBOX_LOCK = threading.Lock()


def read_outbox() -> list[dict]:
    return _read_json(OUTBOX_PATH, [])


def write_outbox(items: list[dict]) -> None:
    _write_json(OUTBOX_PATH, items)


def outbox_append(item: dict) -> None:
    with _OUTBOX_LOCK:
        items = read_outbox()
        items.append(item)
        write_outbox(items)


def outbox_append_once(item: dict, dedupe_key: str, origin: str, look_back: int = 8) -> bool:
    """带去重的追加：最近 look_back 条里已有同 origin 同 key 的就不写。返回是否真的写了。

    查重和写入必须在**同一把锁里**：code 模式一轮里并行调好几个工具时，几个 hook 进程会
    同时打进来，各自查重时谁都还没落盘 → 全部通过 → 同一段话上屏好几次。"""
    with _OUTBOX_LOCK:
        items = read_outbox()
        for x in items[-look_back:]:
            if x.get("origin") == origin and x.get("hook_key") == dedupe_key:
                return False
        items.append(item)
        write_outbox(items)
        return True


def outbox_pending() -> list[dict]:
    return [x for x in read_outbox() if not x.get("delivered")]


def outbox_ack(ids: list[str]) -> None:
    idset = set(ids)
    now = int(time.time())
    with _OUTBOX_LOCK:
        items = read_outbox()
        for x in items:
            if x.get("id") in idset:
                x["delivered"] = True
        # 顺手清理：已送达且超过 7 天的条目删掉，别让 outbox.json 永远变大。
        items = [x for x in items
                 if not x.get("delivered") or now - int(x.get("ts", 0)) < 7 * 86400]
        write_outbox(items)
