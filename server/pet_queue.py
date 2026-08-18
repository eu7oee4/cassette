"""猫的触发与闸（PLAN_pet P2）：猫什么时候醒，醒之前拦什么。

三路触发（引擎面全在 pet_engine，这里只管什么时候调）：
- **直接互动必醒**：pet_engine.interact 同步走，不经这里；算猫的「外部输入」
  ——interact 调 external_for_pet() 清猫的连发计数。
- **房间事件概率醒**：world 事件钩子只有一个（cohabit_queue._on_room_event），
  它在末尾把事件转发给 on_room_event()——作者排除 + 概率 PET_EVENT_PROB +
  最小间隔 + 猫连发上限，全过了才入队，worker 拉起 pet_engine.wake。
- **需求 tick**：worker 每 PET_TICK_SEC 扫一轮——先 enforce()（生存硬地板，
  不走模型，干了正事这轮就不再叠模型醒），再有需求就醒（原因=needs 文本，
  同类需求 NEED_COOLDOWN_SEC 内不重复催）；没需求偶尔溜达（IDLE_PROB）。

闸（全部确定性，口径抄 cohabit_queue）：
- 猫连发上限 PET_CHAIN_N：事件醒来连续 ≤ N，被喂/被撸（interact）清零；
  需求/溜达是自主醒，不是链条的延续——反过来清零（同 cohabit 的 solo 口径）。
- 最小间隔 PET_WAKE_GAP_SEC：热闹房间不对每条事件都烧一次 DeepSeek。
- 错误冷却 ERROR_COOLDOWN_SEC：引擎失败别追着失败（互动照常走，错让工具层报）。

反向闸（char 侧，执行在 cohabit_queue._on_room_event，口径的家在这里）：
- 猫的事件唤 char：概率 PET_TO_CHAR_PROB 且**不重置 char 连发**（external_input
  只有用户路由会调，猫够不着——结构性成立，不用防）；
- **发起者排除**：char 用工具逗猫，猫的反应已在工具结果里同轮返回——反应事件
  不再唤发起者（interaction_guard 在 interact 落反应期间记住发起者）。

worker 独立线程（app lifespan 起），不占 wake.WAKE_EXEC_LOCK——那是 claude -p
的锁，DeepSeek 一次 ~3.5s，不抢。引擎并发由 pet_engine._ENGINE_LOCK 兜底
（interact 与 tick 可能同时摸猫）。状态全在内存：重启即清，丢的只是计时不是事实。
"""
import random
import threading
import time
from contextlib import contextmanager

from typing import Optional

import config
import pet_engine
import pet_store
import pets
import world
from notify import logerr

PET_TICK_SEC = 60            # 需求判定周期
PET_EVENT_PROB = 0.25        # 猫对房间事件的响应概率（拍板：调低——猫爱答不理）
PET_TO_CHAR_PROB = 0.5       # 猫的动静唤 char 的概率（拍板：降权）
PET_CHAIN_N = 3              # 猫的事件连发上限
PET_WAKE_GAP_SEC = 180       # 两次引擎醒来的最小间隔
NEED_COOLDOWN_SEC = 1800     # 同类需求的重复催促间隔（催过一次 30 分钟内不再催）
IDLE_PROB = 0.02             # 没需求时每 tick 的溜达概率（醒着 ~50 分钟一次）
ERROR_COOLDOWN_SEC = 600

_lock = threading.Lock()
_signal = threading.Event()
_pending: dict[str, list[str]] = {}      # pid → 事件醒因（去重合并，单 pending）
_chain: dict[str, int] = {}              # pid → 事件连发计数
_last_wake: dict[str, float] = {}
_need_last: dict[tuple, float] = {}      # (pid, need_kind) → 上次催促 ts
_cooldown: dict[str, float] = {}
_initiator: dict = {"id": None}          # 本轮 interact 的发起者（反应事件不唤他）


# ---------- 发起者护栏（pet_engine.interact 落反应事件期间持有）----------
@contextmanager
def interaction_guard(actor: str):
    _initiator["id"] = actor
    try:
        yield
    finally:
        _initiator["id"] = None


def initiator() -> Optional[str]:
    return _initiator["id"]


def paused() -> bool:
    """跟着机主那个暂停键走——**同一个键管全屋，猫也是屋里的一员**。
    2026-08-19 实录：人按了暂停在打字，猫照样每分钟 tick、照样醒、照样往房间里
    落事件。口径抄 cohabit：暂停只停**执行**（tick/冲队/硬地板），事件照常落盘、
    pending 照常合并，按「开始」一口气恢复；直接互动（喂/撸）不受影响——那是人
    自己的动作，不是 AI 在抢话。
    函数内 import 断环：cohabit_queue 顶层 import 本模块。"""
    import cohabit_queue
    return cohabit_queue.paused()


def external_for_pet(pid: str) -> None:
    """被直接互动 = 猫的外部输入：事件连发计数清零（pet_engine.interact 调）。"""
    with _lock:
        _chain.pop(pid, None)


# ---------- 房间事件 → 猫的概率醒（cohabit_queue._on_room_event 末尾转发）----------
def _event_text(ev: dict) -> str:
    name = world.entity_name(ev.get("actor", ""))
    t = ev.get("type")
    if t == "speech":
        return f"{name}在旁边说话：「{ev.get('text', '')}」（你听不懂内容，只听语气和熟词）"
    if t == "action":
        return f"{name} *{ev.get('text', '')}*"
    return f"（{ev.get('text', '')}）"


def on_room_event(room_id: str, ev: dict) -> None:
    """在世界锁内被调（同 cohabit 钩子）：只做判定 + 入队，快进快出，绝不调引擎。"""
    if not config.COHABIT_ENABLED:
        return
    now = time.time()
    actor = ev.get("actor")
    for pid in pets.ids():
        if pid == actor or world.location_of(pid) != room_id:
            continue
        if now < _cooldown.get(pid, 0) or now - _last_wake.get(pid, 0) < PET_WAKE_GAP_SEC:
            continue
        with _lock:
            if _chain.get(pid, 0) >= PET_CHAIN_N:
                continue
        if random.random() >= PET_EVENT_PROB:
            continue
        text = _event_text(ev)
        with _lock:
            if pid in _pending:
                if text not in _pending[pid]:
                    _pending[pid].append(text)
            else:
                _pending[pid] = [text]
        _signal.set()


# ---------- 执行 ----------
def _wake(pid: str, reasons: list[str], autonomous: bool) -> None:
    """拉起一次猫醒来。autonomous（需求/溜达）不是链条的延续 → 计数清零；
    事件醒来计数 +1。失败进冷却，别追着失败烧。"""
    with _lock:
        if autonomous:
            _chain.pop(pid, None)
        else:
            _chain[pid] = _chain.get(pid, 0) + 1
        _last_wake[pid] = time.time()
    try:
        pet_engine.wake(pid, reasons)
    except Exception as e:
        logerr(f"pet 醒来失败（{pid}，冷却 {ERROR_COOLDOWN_SEC // 60} 分钟）: {e}")
        _cooldown[pid] = time.time() + ERROR_COOLDOWN_SEC


def _drain() -> None:
    while True:
        if paused():
            return         # 暂停：pending 原地攒着（合并去重照常），按开始再冲
        with _lock:
            if not _pending:
                return
            pid, reasons = _pending.popitem()
        _wake(pid, reasons, autonomous=False)


def _tick(now: float) -> None:
    if paused():
        return   # 连硬地板一起停：enforce 也是猫自己动（落进出+动作事件），暂停期
                 # 间不该有任何 AI 侧的动静。阈值判定是无状态的，恢复后当轮就补上。
    for pid in pets.ids():
        try:
            if now < _cooldown.get(pid, 0):
                continue
            if pet_engine.enforce(pid):
                continue   # 硬地板这轮干了正事（吃/拉都是真事件），不再叠模型醒
            s = pet_store.read_state(pid)
            if s.get("asleep"):
                continue   # 睡着：衰减和需求都冻着，自然醒交给精力恢复
            due = [n for n in pet_store.needs(s)
                   if now - _need_last.get((pid, n["kind"]), 0) >= NEED_COOLDOWN_SEC]
            if now - _last_wake.get(pid, 0) < PET_WAKE_GAP_SEC:
                continue
            if due:
                for n in due:
                    _need_last[(pid, n["kind"])] = now
                _wake(pid, [n["text"] for n in due], autonomous=True)
            elif not pet_store.needs(s) and random.random() < IDLE_PROB:
                _wake(pid, ["没什么事，你就是醒着——想溜达就溜达，想找个地方窝着就窝着"],
                      autonomous=True)
        except Exception as e:
            logerr(f"pet tick 出错（{pid}，忽略）: {e}")


def worker_loop() -> None:
    """常驻 worker（daemon 线程，app lifespan 里起）。没注册宠物时空转极便宜
    ——注册团团 + 重启即上线，不用再碰这里。"""
    logerr(f"pet worker 启动（tick={PET_TICK_SEC}s，事件概率={PET_EVENT_PROB}，"
           f"连发上限 N={PET_CHAIN_N}）")
    last_tick = 0.0
    while True:
        _signal.wait(timeout=10)
        _signal.clear()
        try:
            now = time.time()
            _drain()
            if now - last_tick >= PET_TICK_SEC:
                last_tick = now
                _tick(now)
        except Exception as e:
            logerr(f"pet worker 出错（忽略，继续）: {e}")
