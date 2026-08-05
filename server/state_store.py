"""运行时状态存储（纯文件 I/O，不依赖 app，可独立测试）。
所有状态放 server/state/（已 gitignore）。写入走临时文件 + 原子替换，避免半截文件。"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

from typing import Optional

STATE_DIR = Path(__file__).resolve().parent / "state"
STATE_DIR.mkdir(exist_ok=True)

OUTBOX_PATH = STATE_DIR / "outbox.json"
SETTINGS_PATH = STATE_DIR / "settings.json"
RECENT_WINDOW_PATH = STATE_DIR / "recent_window.json"
WAKE_LOG_PATH = STATE_DIR / "wake_log.jsonl"
SCHEDULE_PATH = STATE_DIR / "schedule.json"   # {next_wake_at, last_wake_at}，持久化跨重启

RECENT_WINDOW_N = 300  # 窗口存储容量上限；wake 实际注入条数由 settings.wake_window_n 决定

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


# ---------- settings ----------
def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(_read_json(SETTINGS_PATH, {}))
    return s


def save_settings(s: dict) -> None:
    _write_json(SETTINGS_PATH, s)


# ---------- recent window（最近对话快照）----------
# 醒来时 app 不在场，模型靠这份快照拿上下文（app 仍是历史的唯一主人，这只是后端侧的影子）。
def write_recent_window(messages: list[dict]) -> None:
    _write_json(RECENT_WINDOW_PATH, messages[-RECENT_WINDOW_N:])


def read_recent_window() -> list[dict]:
    return _read_json(RECENT_WINDOW_PATH, [])


# ---------- wake log（醒来极简元数据，append-only）----------
def append_wake_log(entry: dict) -> None:
    with WAKE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_wake_log(limit: Optional[int] = None) -> list[dict]:
    if not WAKE_LOG_PATH.exists():
        return []
    out = []
    for ln in WAKE_LOG_PATH.read_text("utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out[-limit:] if limit else out


# ---------- 表情库清单（app 每次 /chat 存一份，醒来时后端不在 app 手里也能挑表情发）----------
STICKER_CATALOG_PATH = STATE_DIR / "sticker_catalog.json"


def read_sticker_catalog() -> list[dict]:
    return _read_json(STICKER_CATALOG_PATH, [])


def write_sticker_catalog(catalog: list[dict]) -> None:
    _write_json(STICKER_CATALOG_PATH, catalog)


# ---------- 醒来调度（next_wake_at 持久化，跨重启）----------
# 调度器 tick 靠这份落盘：不落盘的话频繁重启会把定时器重置、永远轮不到自主醒来。
def read_schedule() -> dict:
    return _read_json(SCHEDULE_PATH, {})


def write_schedule(d: dict) -> None:
    _write_json(SCHEDULE_PATH, d)


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
