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
房间 id | "away"（出门，数据层第一天就成立：away 的人房间事件与他无关，只剩手机
可用）。曾有过 "hallway"（在楼里但不在任何房间）——UI 侧 2026-08-16 已移除、
数据层 2026-08-17 摘干净：大家都是瞬移走的，两个房间之间没有「路上」。

可见性口径（C1 注入按这个来）：visible_events = 自己本次在场区间内的该房间事件。
离场期间的事件永远看不见——看得见牛奶，不知道谁放的。偷看（全部历史）是用户的
上帝视角，走 read_events，不产生事件、AI 不感知。

并发：写入方会有 HTTP 路由 / wake 执行线程多个，读改写全走 _LOCK（RLock——
move 里要套 append_event）。文件写入同 state_store：唯一临时名 + 原子替换。
"""
import json
import os
import re
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
    """没记录过位置的实体：有自己卧室就在卧室，没有就先站客厅（走廊摘除后
    公共空间是唯一不凭空塞进谁房间的落点）。"""
    for rid, r in reg.items():
        if r.get("type") == "bedroom" and r.get("owner") == entity:
            return rid
    return "living_room"


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
def _append_experience(room_id: str, ev: dict) -> None:
    """事件落盘的同时写进每个**在场角色**的经历流（state/characters/<id>/experience.jsonl）
    ——「他看见了什么」在发生那一刻定格成第一人称流水，注入时按 ts 与手机线程/内心
    合并成一条经历时间线（PLAN「recent_window 泛化成第一人称经历流」的落地）。
    作者自己也写（他做的事当然是他经历的一部分）；用户不写（用户的经历面是 app 本身）。
    写放大 = 事件数 × 在场角色数，量级无压力。"""
    for e in occupants(room_id):
        if e == USER_ID:
            continue
        p = state_store.char_state_dir(e) / "experience.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**ev, "room": room_id}, ensure_ascii=False) + "\n")


def read_experience(cid: str, limit: Optional[int] = None) -> list[dict]:
    p = state_store.char_state_dir(cid) / "experience.jsonl"
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


def append_event(room_id: str, etype: str, actor: str, text: str,
                 kind: Optional[str] = None, with_: Optional[list] = None) -> dict:
    """写一条房间事件并返回它。**这里不做在场校验**——校验是 act/move/state_change
    这些动作入口的事，引擎内部（比如 move 写进出通知）要能直接落笔。
    with_＝这条事件同时作用到的其他实体（抱着谁进出）：机制字段，可见性起点认它。"""
    ev = {"id": uuid.uuid4().hex[:8], "ts": int(time.time()),
          "type": etype, "actor": actor, "text": text}
    if kind:
        ev["kind"] = kind
    if with_:
        ev["with"] = list(with_)
    with _LOCK:
        d = ROOMS_DIR / room_id
        d.mkdir(parents=True, exist_ok=True)
        with (d / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        try:
            _append_experience(room_id, ev)
        except Exception as e:
            # 经历流写不上不能反噬事件本身：事件已落盘就是发生了。
            print(f"[world] 经历流写入失败（忽略）: {e}", flush=True)
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
    """entity 的可见事件：
    - 在场 → 本次在场区间（最近一次自己进屋的 enter 事件起，含那条）；从没有
      enter 事件（开局就被放在屋里）→ 全部历史，他确实一直在场。
    - 不在场 → **最后一段在场区间**（enter..leave 含两端），只读回看——亲历过的
      不因为离开而蒸发（被抱走的人补看上一屋不该要上帝视角，2026-08-17 机主）。
      离开之后发生的事照旧永远不在这里。没有 leave 记录（从没进来过，或数据缺口
      判不出区间）→ 空列表，宁可少给不多给。
    被抱着进出的（自己在 enter/leave 事件的 with 里）同样算自己的进出场。"""
    evs = read_events(room_id)

    def mine(e: dict, kind: str) -> bool:
        return e.get("type") == "system" and e.get("kind") == kind \
            and (e.get("actor") == entity or entity in (e.get("with") or []))

    if location_of(entity) == room_id:
        start = next((i for i in range(len(evs) - 1, -1, -1)
                      if mine(evs[i], "enter")), 0)
        out = evs[start:]
    else:
        end = next((i for i in range(len(evs) - 1, -1, -1)
                    if mine(evs[i], "leave")), None)
        if end is None:
            return []
        start = next((i for i in range(end - 1, -1, -1)
                      if mine(evs[i], "enter")), 0)
        out = evs[start:end + 1]
    return out[-limit:] if limit else out


# ---------- 物理引擎：move / act / 地点状态 ----------
def can_enter(entity: str, room_id: str) -> tuple[bool, str]:
    r = room(room_id)
    if not r.get("lock"):
        return True, ""
    if entity == r.get("owner") or entity in (r.get("keys") or []):
        return True, ""
    return False, "locked"


def move(entity: str, to: str, carry: Optional[str] = None) -> dict:
    """瞬移（不考虑空间路径），但过门禁。返回结果 dict，**门锁着不是异常是结局**：
    {"ok": False, "reason": "locked", "text": 给补醒注入用的一句话}。
    成功时旧房间落 leave、新房间落 enter（away 不是房间，没有事件流）。

    carry＝抱着谁一起走：被抱者必须与移动者同处一室（不同室 ValueError——上层该先
    验，这里失手就有声报错）；门禁按**移动者**判（开门的是他，被抱的跟着进）；
    两人位置同更，事件只写一条「X 抱着 Y 进来了/离开了」+ with 字段（可见性起点
    认 with，被抱者从那条事件起看得见新屋）。谁能抱谁是上层的政策（cohabit 一期
    只放行抱用户），物理层只管同室这一条。"""
    with _LOCK:
        frm = location_of(entity)
        if carry:
            if carry == entity or carry not in entity_ids():
                raise ValueError(f"抱不了：{carry}")
            if location_of(carry) != frm:
                raise ValueError(f"{entity_name(carry)} 不在你身边，抱不着")
        if to == frm:
            return {"ok": True, "from": frm, "to": to, "noop": True}
        if to != AWAY:
            r = room(to)   # 不认识的房间 → KeyError，调用方处理
            ok, reason = can_enter(entity, to)
            if not ok:
                return {"ok": False, "from": frm, "to": to, "reason": reason,
                        "text": f"「{r['name']}」的门锁着，没进去"}
        name = entity_name(entity)
        reg = load_registry()
        _set_location(entity, to)
        if carry:
            _set_location(carry, to)
        tail = f" 抱着 {entity_name(carry)}" if carry else ""
        with_ = [carry] if carry else None
        if frm in reg:
            append_event(frm, "system", entity, f"{name}{tail} 离开了",
                         kind="leave", with_=with_)
        if to in reg:
            append_event(to, "system", entity, f"{name}{tail} 进来了",
                         kind="enter", with_=with_)
        out = {"ok": True, "from": frm, "to": to}
        if carry:
            out["carry"] = carry
        return out


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


# 混写表达：一段文本里 *斜体* 是动作、其余是说话，按出现顺序拆成事件序列——
# 「*坐下* 今天好冷 *拉过毯子* 你也过来」→ action/speech/action/speech 四条。


def split_mixed(text: str) -> list[tuple[str, str]]:
    """混写文本 → [(类型, 内容)]，类型 action|speech，保序、去空。纯解析无 IO，可独立测。

    按星号切段配对（**允许跨行**——模型爱把 * 和内容换行写，正则版 [^*\\n] 配不上，
    落进气泡的孤星号和错位实锤过 2026-08-16）；星号奇数个时最后一个当字面丢弃、
    尾段并回说话——错一个星号只脏一段，不把后面全带歪。段内空白压平
    （动作/说话里的换行都是排版噪音，事件本身是最小单元）。"""
    parts = (text or "").split("*")
    if len(parts) % 2 == 0 and len(parts) > 1:   # 星号奇数个：未闭合尾段并回上一段
        parts = parts[:-2] + [parts[-2] + parts[-1]]
    out: list[tuple[str, str]] = []
    for i, seg in enumerate(parts):
        seg = " ".join(seg.split())
        if seg:
            out.append(("action" if i % 2 == 1 else "speech", seg))
    return out


def act_mixed(entity: str, room_id: str, text: str) -> dict:
    """混写表达入口（动作+说话可交错）。空文本 / 拆完全空 → ValueError。"""
    segs = split_mixed((text or "").strip())
    if not segs:
        raise ValueError("说点什么或做点什么")
    with _LOCK:
        room(room_id)
        if location_of(entity) != room_id:
            raise NotPresent(f"{entity_name(entity)} 不在这个房间")
        out = [append_event(room_id, kind, entity, seg) for kind, seg in segs]
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
        # 通知格式三操作统一（机主 2026-08-16）：「谁「那段文字」」——只报结果不报差异。
        # remove 的文字是叙事（「把空碗收走了」）：快照直接删条目，叙事进事件流。
        if op == "add":
            if not text:
                raise ValueError("add 需要 text")
            entry = {"id": uuid.uuid4().hex[:8], "text": text,
                     "author": entity, "since": int(time.time())}
            entries.append(entry)
            notice = f"{name}「{text}」"
        elif op in ("edit", "remove"):
            entry = next((x for x in entries if x.get("id") == entry_id), None)
            if entry is None:
                raise KeyError(f"没有这条地点状态：{entry_id}")
            if op == "edit":
                if not text:
                    raise ValueError("edit 需要 text")
                notice = f"{name}「{text}」"
                entry.update(text=text, author=entity, since=int(time.time()))
            else:
                entries.remove(entry)
                # 没写叙事＝静默清理（机主 2026-08-17）：快照删掉就完了，**不落事件**
                # ——在场者不被通知、不触发醒来。收拾东西值得说一嘴的才写叙事。
                notice = f"{name}「{text}」" if text else None
        else:
            raise ValueError(f"不认识的操作：{op}")
        _write_json(REGISTRY_PATH, reg)
        ev = append_event(room_id, "system", entity, notice, kind="state") if notice else None
        return {"entry": entry, "event": ev,
                "over_cap": len(entries) > ROOM_STATE_SOFT_CAP}
