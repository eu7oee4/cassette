"""待回应交互（抱人邀约，2026-08-17 机主拍板）。

形状：发起 → 对方回应窗口 → 结果补醒。后置的敲门状态机（knock → 主人回合 →
open/超时 → 敲门人收结果）与这里同形，落地时复用这条路。一期只有 carry 邀约
（AI 想抱用户走）：

- AI 醒来轮写了 MOVE+CARRY → **移动不执行**，落一个 pending 邀约 + 一条房间系统
  事件「X 想抱Y去…」——在场的其他人靠这条事件醒来天然获得插话窗口（作者排除照旧）；
- 用户在 app 里应答（POST /world/carry_offer）：答应 → 这时才 world.move(carry)，
  发起人收 move_result 补醒（锁着的门照旧可能失败，成功失败同一条路）；
  拒绝 → 落「没让抱」系统事件 + 拒绝补醒；
- 没反应（OFFER_TTL_SEC 超时，worker 每轮顺手扫）/ 任一方先挪了地方 → 作废补醒。

状态在内存（口径同 cohabit_queue 的 pending：重启即清——丢邀约不丢事实，
人都还在原地，再抱一次就是了）。被抱的只有用户一个人 → 全局至多一个 pending。

import 依赖：cohabit 从顶层 import 这里，这里回头要 cohabit.move_result_reason 和
cohabit_queue.enqueue——函数内 lazy import 断环，别在模块顶互相拽。
锁序：本模块 _lock 在最外，里面才碰 world 的锁；world 永远不回头调这里。
"""
import threading
import time
import uuid
from typing import Optional

import world
from notify import logerr

OFFER_TTL_SEC = 120     # 回应窗口：超时 = 没反应（机主可能压根没在看手机）

_lock = threading.Lock()
_offer: Optional[dict] = None   # {id, actor, to, room, move_motion, deadline}


def _enqueue(cid: str, reason: dict) -> None:
    """结果补醒入队（system=True：过连发上限——用户应答那条路在 API 层已 external_input
    清零，拦不住的只有超时作废这种真·系统触发，被闸掉就闸掉）。测试打桩点。"""
    import cohabit_queue
    cohabit_queue.enqueue(cid, reason, system=True)


def _room_name(rid: str) -> str:
    try:
        return world.room(rid).get("name", rid)
    except KeyError:
        return rid


def _void_reason(o: dict, why: str) -> dict:
    u = world.entity_name(world.USER_ID)
    return {"kind": "carry_void",
            "text": f"你想抱{u}去「{_room_name(o['to'])}」，{why}，没抱成——"
                    f"你还在原地。独自过去、留下、或者说点别的，都行。"}


def create(actor: str, to: str, move_motion: str = "") -> dict:
    """AI 发起抱人邀约（cohabit 醒来轮的 MOVE+CARRY 落到这里，移动不执行）。
    调用方已验证用户与 actor 同屋。已有 pending 时顶掉旧的：同人重复发起只是刷新；
    换了人则旧发起人收作废补醒（结构上极难发生，发生了也别静默吞）。"""
    global _offer
    room_id = world.location_of(actor)
    with _lock:
        old = _offer
        _offer = {"id": uuid.uuid4().hex[:8], "actor": actor, "to": to,
                  "room": room_id, "move_motion": move_motion,
                  "deadline": int(time.time()) + OFFER_TTL_SEC}
        cur = dict(_offer)
    if old and old["actor"] != actor:
        _enqueue(old["actor"], _void_reason(old, "被别的动静岔开了"))
    # 邀约是房间里真实发生的事：落系统事件，在场者（比如另一个 AI）看得见、
    # 会被事件醒来叫起来——插话窗口不用单独设计。
    world.append_event(room_id, "system", actor,
                       f"{world.entity_name(actor)} 想抱"
                       f"{world.entity_name(world.USER_ID)}去「{_room_name(to)}」",
                       kind="carry_offer")
    return cur


def api_view() -> Optional[dict]:
    """给 /world 和 /rooms 的视图（带显示名，UI 直接用）。过期的当没有——
    作废补醒交给 sweep，这里只管别把死邀约摆上界面。"""
    with _lock:
        o = dict(_offer) if _offer else None
    if not o or time.time() > o["deadline"]:
        return None
    return {"id": o["id"], "actor": o["actor"],
            "actor_name": world.entity_name(o["actor"]),
            "room": o["room"], "to": o["to"], "to_name": _room_name(o["to"]),
            "deadline": o["deadline"]}


def respond(offer_id: str, accept: bool) -> dict:
    """用户应答。答应 → 这时才真的 move+carry（门禁照过，锁着照样进不去）；
    拒绝 → 落事件 + 拒绝补醒。邀约不在/对不上号/世界变了 → ValueError
    （API 层转 409——「已经过去了」是状况不是错误，但按钮白按了得让 UI 知道）。"""
    global _offer
    with _lock:
        o = _offer
        if not o or o["id"] != offer_id:
            raise ValueError("这个邀约已经不在了")
        _offer = None
    import cohabit
    actor, u = o["actor"], world.USER_ID
    u_name = world.entity_name(u)
    # 应答落地前世界可能变了：任一方已不在发起时的房间 → 作废（发起人收补醒）。
    if world.location_of(actor) != o["room"] or world.location_of(u) != o["room"]:
        _enqueue(actor, _void_reason(o, "你们已经不在原来的地方"))
        raise ValueError("你们已经不在一个房间了，这个邀约失效了")
    if not accept:
        world.append_event(o["room"], "system", u,
                           f"{u_name} 摇了摇头，没让抱", kind="carry_result")
        _enqueue(actor, {"kind": "carry_declined",
                         "text": f"{u_name}摇头没让抱——你还在原地。"
                                 f"独自过去、留下、或者说点别的，都行。"})
        return {"ok": True, "accepted": False}
    try:
        mv = world.move(actor, o["to"], carry=u)
    except ValueError as e:
        # 上面刚验过同屋还失手 = 并发缝里世界又变了：作废，别把用户的点击变成 500。
        logerr(f"carry 邀约落地失手（{e}），作废")
        _enqueue(actor, _void_reason(o, "落地那一下世界刚好变了"))
        raise ValueError("没抱成——世界刚好变了")
    if mv["ok"] and o.get("move_motion"):
        # 进场自带的样子（MOVE 第二段）：口径同 do_cohabit_wake，enter 后紧跟一条动作。
        try:
            world.act(actor, mv["to"], action=o["move_motion"])
        except Exception as e:
            logerr(f"carry 邀约进场动作没写上（忽略）: {e}")
    reason = cohabit.move_result_reason(mv)
    if mv["ok"]:
        reason["text"] = f"{u_name}答应了让你抱。" + reason["text"]
    _enqueue(actor, reason)
    return {"ok": True, "accepted": bool(mv["ok"]), "move": mv}


def sweep(now: Optional[float] = None) -> None:
    """worker 每轮顺手扫：超时 / 任一方先挪了地方 → 作废 + 发起人补醒。"""
    global _offer
    now = now or time.time()
    with _lock:
        o = _offer
        if not o:
            return
        why = None
        if now > o["deadline"]:
            why = f"{world.entity_name(world.USER_ID)}一直没反应"
        else:
            try:
                if world.location_of(o["actor"]) != o["room"]:
                    why = "你自己先挪了地方"
                elif world.location_of(world.USER_ID) != o["room"]:
                    why = f"没等到回应，{world.entity_name(world.USER_ID)}就先走开了"
            except Exception as e:
                logerr(f"carry 邀约扫除时探位置失败（作废处理）: {e}")
                why = "世界变了"
        if why is None:
            return
        _offer = None
    _enqueue(o["actor"], _void_reason(o, why))
