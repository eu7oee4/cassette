"""同居世界（大房子）数据层 + 物理引擎（PLAN_cohabit C0）。

服务端当物理引擎：写入房间要求发言者在场、move 过门禁校验、地点状态改动落系统通知。
本模块只管世界本身（谁在哪 / 房间里发生了什么 / 这里有什么），不碰醒来——
事件怎么触发醒来是 C2 的事，这里一根线都不接。

存储（全在 state/，gitignore）：
    state/world.json             位置权威：per-entity location（唯一一份真相，
                                 「房间里有谁」是查询结果不是存储字段）
    state/rooms/registry.json    房间注册表：{id: {name, floor, type, owner?, lock,
                                 keys, state}}。lock/keys 一期默认全 0/空，要锁就手编
                                 这份文件（give_key 交互后置）。state = 地点状态快照，
                                 条目 {id, text, author, since}——「现在这里有什么」，
                                 不是日志：收拾掉牛奶 = remove 那条，快照只留结果。
    state/rooms/<id>/events.jsonl  每房间事件流，append-only。三类：
                                 system（进出/状态改动，带 kind）/ action（*斜体动作*）
                                 / speech（对话）。

实体 = 用户（固定 id "user"）+ 全部注册角色（characters.ids()）。位置取值 =
房间 id | "hallway"（在楼里但不在任何房间，用户点「离开」落这儿）| "away"（出门，
数据层第一天就成立：away 的人房间事件与他无关，只剩手机可用）。

可见性口径（C1 注入按这个来）：visible_events = 自己本次在场区间内的该房间事件。
离场期间的事件永远看不见——看得见牛奶，不知道谁放的。偷看（全部历史）是用户的
上帝视角，走 read_events，不产生事件、AI 不感知。

并发：写入方会有 HTTP 路由 / wake 执行线程多个，读改写全走 _LOCK（RLock——
move 里要套 append_event）。文件写入同 state_store：唯一临时名 + 原子替换。
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

from typing import Optional

import characters
import config
import state_store

USER_ID = "user"
AWAY = "away"
HALLWAY = "hallway"

ROOMS_DIR = state_store.STATE_DIR / "rooms"
REGISTRY_PATH = ROOMS_DIR / "registry.json"
WORLD_PATH = state_store.STATE_DIR / "world.json"

# 地点状态快照的软上限：超了不硬删，C1 注入时提醒收敛（防膨胀口径见 PLAN_cohabit）。
ROOM_STATE_SOFT_CAP = 10
# AI 每轮 act 附带的状态改动条数上限（C1 解析 ACTION 时执行，这里只是口径的家）。
MAX_STATE_OPS_PER_ACT = 3

_LOCK = threading.RLock()

# 事件钩子（C2 接线口）：append_event 落盘后调它一次，签名 (room_id, event)。
# 默认 None = 世界不惊动任何人（C0 口径不变）；cohabit_queue.install() 才挂上。
# 钩子在世界锁**内**被调（act/move 是组合动作，锁内保证事件序完整）——
# 挂进来的实现只准做入队这类快操作，绝不准回头调 world 的写函数。
event_hook = None


class NotPresent(Exception):
    """发言者不在这个房间——物理引擎拒绝写入（API 层转 409）。"""


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    # 同 state_store：唯一临时名（pid+uuid）+ 原子替换，防半截文件和并发撞名。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


# ---------- 实体 ----------
def entity_ids() -> list[str]:
    return [USER_ID] + characters.ids()


def entity_name(entity: str) -> str:
    """事件文本里的称呼：用户用设置里的昵称，角色用 display_name。"""
    if entity == USER_ID:
        return config.user_name()
    try:
        return characters.display_name(entity)
    except KeyError:
        return entity


# ---------- 房间注册表 ----------
def load_registry() -> dict:
    return _read_json(REGISTRY_PATH, {})


def room(room_id: str) -> dict:
    """注册表条目。不认识的房间 → KeyError（API 层转 404），口径同 characters.resolve：
    静默把打错的房间名当成某个房间，事件会进错房。"""
    reg = load_registry()
    if room_id not in reg:
        raise KeyError(f"没有这个房间：{room_id}")
    return reg[room_id]


def ensure_world() -> None:
    """启动时补齐注册表和 world.json（幂等，只在缺文件时写默认值——
    注册表是机主可手编的运行时数据，存在就绝不覆盖）。大房子一期：
    二层三卧 + 浴室，一层客厅。名字用创建当时的称呼渲染，之后手编为准。"""
    ROOMS_DIR.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_PATH.exists():
        user = config.user_name()
        rooms: dict = {
            "mm_room": {"name": f"{user}的房间", "floor": 2, "type": "bedroom",
                        "owner": USER_ID, "lock": 0, "keys": [], "state": []},
            "wash_room": {"name": "浴室", "floor": 2, "type": "functional",
                          "owner": USER_ID, "lock": 0, "keys": [], "state": []},
            "living_room": {"name": "客厅", "floor": 1, "type": "common",
                            "lock": 0, "keys": [], "state": []},
        }
        for cid in characters.ids():
            rooms[f"{cid}_room"] = {
                "name": f"{characters.display_name(cid)}的房间", "floor": 2,
                "type": "bedroom", "owner": cid, "lock": 0, "keys": [], "state": []}
        _write_json(REGISTRY_PATH, rooms)
    if not WORLD_PATH.exists():
        reg = load_registry()
        _write_json(WORLD_PATH, {e: {"location": _default_location(e, reg)}
                                 for e in entity_ids()})


def _default_location(entity: str, reg: dict) -> str:
    """没记录过位置的实体：有自己卧室就在卧室，没有就在走廊（后注册的角色先站走廊，
    别凭空塞进谁的房间）。"""
    for rid, r in reg.items():
        if r.get("type") == "bedroom" and r.get("owner") == entity:
            return rid
    return HALLWAY


# ---------- 位置（world.json 是唯一权威）----------
def world_snapshot() -> dict:
    """全体实体的位置（缺记录的现场补默认值，不落盘——落盘等他真的动）。"""
    with _LOCK:
        w = _read_json(WORLD_PATH, {})
        reg = load_registry()
        return {e: {"location": (w.get(e) or {}).get("location")
                    or _default_location(e, reg)}
                for e in entity_ids()}


def location_of(entity: str) -> str:
    return world_snapshot()[entity]["location"]


def occupants(room_id: str) -> list[str]:
    """房间里有谁 = 查询结果，不是存储字段（不搞两份真相）。"""
    snap = world_snapshot()
    return [e for e in entity_ids() if snap[e]["location"] == room_id]


def _set_location(entity: str, loc: str) -> None:
    with _LOCK:
        w = _read_json(WORLD_PATH, {})
        w.setdefault(entity, {})["location"] = loc
        _write_json(WORLD_PATH, w)


# ---------- 事件流（每房间 append-only）----------
def append_event(room_id: str, etype: str, actor: str, text: str,
                 kind: Optional[str] = None) -> dict:
    """写一条房间事件并返回它。**这里不做在场校验**——校验是 act/move/state_change
    这些动作入口的事，引擎内部（比如 move 写进出通知）要能直接落笔。"""
    ev = {"id": uuid.uuid4().hex[:8], "ts": int(time.time()),
          "type": etype, "actor": actor, "text": text}
    if kind:
        ev["kind"] = kind
    with _LOCK:
        d = ROOMS_DIR / room_id
        d.mkdir(parents=True, exist_ok=True)
        with (d / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if event_hook:
            try:
                event_hook(room_id, ev)
            except Exception as e:
                # 唤醒失败不能反噬物理引擎：事件已落盘就是发生了，钩子的事钩子自己扛。
                print(f"[world] event_hook 出错（忽略）: {e}", flush=True)
    return ev


def read_events(room_id: str, limit: Optional[int] = None) -> list[dict]:
    """全部历史（偷看 = 用户上帝视角走这个；AI 永远不走）。"""
    p = ROOMS_DIR / room_id / "events.jsonl"
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


def visible_events(room_id: str, entity: str,
                   limit: Optional[int] = None) -> list[dict]:
    """entity 的可见事件 = 本次在场区间（最近一次自己进屋的 enter 事件起，含那条）。
    人不在这个房间 → 空列表。从没有 enter 事件（开局就被放在屋里）→ 全部历史，
    他确实一直在场。"""
    if location_of(entity) != room_id:
        return []
    evs = read_events(room_id)
    start = 0
    for i in range(len(evs) - 1, -1, -1):
        e = evs[i]
        if e.get("type") == "system" and e.get("kind") == "enter" \
                and e.get("actor") == entity:
            start = i
            break
    out = evs[start:]
    return out[-limit:] if limit else out


# ---------- 物理引擎：move / act / 地点状态 ----------
def can_enter(entity: str, room_id: str) -> tuple[bool, str]:
    r = room(room_id)
    if not r.get("lock"):
        return True, ""
    if entity == r.get("owner") or entity in (r.get("keys") or []):
        return True, ""
    return False, "locked"


def move(entity: str, to: str) -> dict:
    """瞬移（不考虑空间路径），但过门禁。返回结果 dict，**门锁着不是异常是结局**：
    {"ok": False, "reason": "locked", "text": 给补醒注入用的一句话}。
    成功时旧房间落 leave、新房间落 enter（hallway/away 不是房间，没有事件流）。"""
    with _LOCK:
        frm = location_of(entity)
        if to == frm:
            return {"ok": True, "from": frm, "to": to, "noop": True}
        if to not in (AWAY, HALLWAY):
            r = room(to)   # 不认识的房间 → KeyError，调用方处理
            ok, reason = can_enter(entity, to)
            if not ok:
                return {"ok": False, "from": frm, "to": to, "reason": reason,
                        "text": f"「{r['name']}」的门锁着，没进去"}
        name = entity_name(entity)
        reg = load_registry()
        _set_location(entity, to)
        if frm in reg:
            append_event(frm, "system", entity, f"{name} 离开了", kind="leave")
        if to in reg:
            append_event(to, "system", entity, f"{name} 进来了", kind="enter")
        return {"ok": True, "from": frm, "to": to}


def act(entity: str, room_id: str, action: str = "", speech: str = "") -> dict:
    """在房间里表达：*斜体动作* + 对话（可一空，不可全空）。
    只有在场才写得进——房间号和实际位置对不上就 NotPresent（防 UI 停留在旧页面、
    或 AI 的 ACTION 带着过时前提）。"""
    action = (action or "").strip()
    speech = (speech or "").strip()
    if not action and not speech:
        raise ValueError("action 和 speech 至少要有一个")
    with _LOCK:
        room(room_id)
        if location_of(entity) != room_id:
            raise NotPresent(f"{entity_name(entity)} 不在这个房间")
        out = []
        if action:
            out.append(append_event(room_id, "action", entity, action))
        if speech:
            out.append(append_event(room_id, "speech", entity, speech))
        return {"room": room_id, "events": out}


def room_state(room_id: str) -> list[dict]:
    return room(room_id).get("state") or []


def state_change(entity: str, room_id: str, op: str,
                 entry_id: Optional[str] = None, text: Optional[str] = None) -> dict:
    """改地点状态快照（add / edit / remove），改动落系统通知——谁改的可见。
    快照语义：收拾掉牛奶 = remove，叙事进事件流（act），快照只留结果。
    在场才能改：用户的编辑入口在房间视图副栏，AI 的在 act 的可选字段，口径一致。"""
    text = (text or "").strip()
    with _LOCK:
        reg = load_registry()
        if room_id not in reg:
            raise KeyError(f"没有这个房间：{room_id}")
        if location_of(entity) != room_id:
            raise NotPresent(f"{entity_name(entity)} 不在这个房间")
        entries = reg[room_id].setdefault("state", [])
        name = entity_name(entity)
        if op == "add":
            if not text:
                raise ValueError("add 需要 text")
            entry = {"id": uuid.uuid4().hex[:8], "text": text,
                     "author": entity, "since": int(time.time())}
            entries.append(entry)
            notice = f"{name} 添加了「{text}」"
        elif op in ("edit", "remove"):
            entry = next((x for x in entries if x.get("id") == entry_id), None)
            if entry is None:
                raise KeyError(f"没有这条地点状态：{entry_id}")
            if op == "edit":
                if not text:
                    raise ValueError("edit 需要 text")
                notice = f"{name} 把「{entry['text']}」改成了「{text}」"
                entry.update(text=text, author=entity, since=int(time.time()))
            else:
                entries.remove(entry)
                notice = f"{name} 清掉了「{entry['text']}」"
        else:
            raise ValueError(f"不认识的操作：{op}")
        _write_json(REGISTRY_PATH, reg)
        ev = append_event(room_id, "system", entity, notice, kind="state")
        return {"entry": entry, "event": ev,
                "over_cap": len(entries) > ROOM_STATE_SOFT_CAP}
