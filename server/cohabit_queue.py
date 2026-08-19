"""同居世界的触发与闸（PLAN_cohabit C2）：谁在什么时候被叫醒，醒之前拦什么。

三路触发（注入一样，只有原因不同——执行全在 cohabit.do_cohabit_wake）：
- 事件醒来：房间里出现新事件 → 唤醒在场 AI。**作者排除**：自己产生的事件不唤醒自己
  （否则说一句话就自唤醒，单人即死循环）；用户永远不被"唤醒"（用户不是 AI）。
- 自主醒来：独处即可（任何地点）——所在地点只有自己时按概率掷；他自己定的 NEXT
  到点必醒（不要求独处）。预算（wake_daily_budget）只管这一路。
- 手机消息醒来：现有流式聊天回合保留不动，只是回复可附带 [[move:房间id]]——
  move 在轮末执行，结果补醒从这里入队。

队列四件套（全部确定性，不指望模型自觉）：
- **执行锁并发 = 1**：单 worker 线程串行执行，两个 AI 绝不叠着起 claude -p；
- **注入在执行开始时组装**：do_cohabit_wake 进场才拼 prompt，队末即正确，插队不存在；
- **每 AI 至多一个 pending**：已有 pending 时新触发只往原因清单追加（同文去重）；
  触发落在该 AI 生成中 → 落成下一个 pending（执行开始时 pending 已弹出）；
- **连发上限（入队处拦）**：无新外部输入（用户发言/动作/移动/状态编辑/手机消息）时，
  一个 AI 连续被系统触发的醒来 ≤ config.COHABIT_CHAIN_N（含事件醒来与 move 补醒链）。
  不入队才省 token——入队后醒来选 none 已经烧了。自主醒来不受这道闸（它归预算管），
  执行时也**不计数、反而清零**：它不是任何链条的延续，计数它等于让睡着的机主把 AI
  锁死在原地（见 _pop_next）。

总开关 config.COHABIT_ENABLED（默认关）：关着时 install 不挂钩子、worker 不启动、
所有入口一进来就返回——C2 全部接好线但不上电，上电是 C4 的事。

计数器/队列都在内存里：重启即清零。丢 pending 不丢事实——事件在盘上，下一个触发
自然再来；连发计数清零最坏多聊几轮，比落盘状态机简单得多。
"""
import random
import threading
import time

from typing import Optional

import characters
import cohabit
import config
import offers
import pet_queue
import pets
import pipeline
import state_store
import wake
import world
from notify import logerr

SOLO_TICK_SEC = 60          # 自主醒来的判定周期（概率按此缩放，口径同 wake.FREQ_PROB_15）
_WORKER_WAIT_SEC = 30       # worker 空等超时：兼作 chat 避让后的重试间隔

_lock = threading.Lock()
_signal = threading.Event()
_pending: dict[str, list[dict]] = {}   # cid → 原因清单（至多一个 pending）
_order: list[str] = []                 # FIFO
_syswake_run: dict[str, int] = {}      # cid → 连续系统触发醒来计数（外部输入清零）
_gate_hit: dict[str, bool] = {}        # 连发上限的日志只在撞上那次打一条
_defer_hit: dict[str, bool] = {}       # code 会话延后的日志同理：进入延后那次打一条
_executing: dict = {"cid": None}       # 正在执行醒来的角色（房间视图「正在回应」动画用）
# 用户的暂停键（2026-08-16）：打字需要时间，不按暂停的话插嘴总是慢 AI 两轮。
# 暂停 = 只停**执行**：正在生成的那轮照常说完，事件照常落盘、pending 照常合并，
# 按开始一口气恢复。内存态：后端重启即恢复运行（UI 从 /world 读真相，不会骗人）。
_paused: dict = {"on": False}
# 最近一次「把队列按停」的原因（模型那头过载）：UI 拿去摆在房间/房子页上。
# 人按「开始」= 知道了，清掉。内存态，重启即清（跟 _paused 同口径）。
_last_error: dict = {"text": "", "ts": 0}


def executing() -> Optional[str]:
    return _executing["cid"]


def paused() -> bool:
    return _paused["on"]


def last_error() -> Optional[dict]:
    return dict(_last_error) if _last_error["text"] else None


def set_paused(on: bool) -> None:
    _paused["on"] = bool(on)
    logerr(f"cohabit 队列{'暂停（正在生成的说完为止，之后攒着）' if on else '恢复（开始冲队）'}")
    if not on:
        _last_error.update(text="", ts=0)   # 人按开始 = 看见了，错误横幅收掉
        _signal.set()   # 恢复那一脚立刻冲队，不等超时

# code 会话探测缓存（探一次是 tmux 子进程，worker 冲队时别每 pop 都探）。
_CODE_PROBE_SEC = 5
_code_cache: dict = {"ts": 0.0, "owner": None}


def _code_owner():
    """电脑前的人（缓存 5 秒）：会话活着 → 归属角色 id，没有 → None。
    这个人的醒来延后到收工——工作入迷的人不接收房间信号，事实照常落盘、
    pending 照常合并，会话一关整批补醒（选项 3 的口径，2026-08-16 拍板）。"""
    now = time.time()
    if now - _code_cache["ts"] > _CODE_PROBE_SEC:
        _code_cache["ts"] = now
        cc = cohabit.coding_char()
        _code_cache["owner"] = cc[0] if cc else None
    return _code_cache["owner"]


def code_session_closed() -> None:
    """code/game 会话收摊（/code/stop 调）：探测缓存作废 + 踢 worker 立刻冲队——
    归属角色攒了一会话的 pending，这一脚让补醒秒级到，不用等 30s 超时兜底。
    会话自退/崩溃没有这一脚，靠 worker 超时 + 缓存过期照样能冲，只是慢半拍。"""
    _code_cache["ts"] = 0.0
    _defer_hit.clear()
    _signal.set()


# ---------- 入队与闸 ----------
def enqueue(cid: str, reason: dict, system: bool = True, force: bool = False) -> bool:
    """入队一个醒来原因。system=True 的（事件/move 补醒）过连发上限和错误冷却；
    自主醒来（solo/scheduled）传 False——它归预算管，闸在 _solo_check 里。

    force=True：机主手点的导演口（/world/nudge）——冷却拦的是「模型一直挂、事件流
    每条都硬起一次注定失败的子进程」，人手动点的一次是明确指令，值得再试一次
    （2026-08-17：三次点名全被冷却静默吞掉，机主那头只看到弹窗关了）。
    force 不只是绕过，是**把这条冷却解除**：不然这轮醒来里的 MOVE 补醒会被同一条
    冷却拦掉，人瞬移过去就哑了。再失败的话 _drain 会重新压 30 分钟，自愈。"""
    if not config.COHABIT_ENABLED:
        return False
    if system:
        # 错误冷却：模型持续失败时别对着事件流每条都硬起一次注定失败的子进程。
        if force:
            _clear_cooldown(cid)
        elif time.time() < float(state_store.read_schedule(cid).get("cooldown_until") or 0):
            return False
        with _lock:
            if _syswake_run.get(cid, 0) >= config.COHABIT_CHAIN_N:
                if not _gate_hit.get(cid):
                    logerr(f"cohabit 连发上限（{cid}）：连续 {config.COHABIT_CHAIN_N} 次"
                           f"系统触发醒来，之后的不再入队（有新外部输入即恢复）")
                    _gate_hit[cid] = True
                return False
            _push(cid, reason)
    else:
        with _lock:
            _push(cid, reason)
    _signal.set()
    return True


def _clear_cooldown(cid: str) -> None:
    """抹掉错误冷却（只有机主点名走这条）。没冷却就什么都不写。"""
    with state_store.SCHEDULE_LOCK:
        sched = state_store.read_schedule(cid)
        if sched.get("cooldown_until"):
            logerr(f"cohabit：机主点名，解除 {cid} 的错误冷却")
            sched.pop("cooldown_until", None)
            state_store.write_schedule(sched, cid)


def _push(cid: str, reason: dict) -> None:
    if cid in _pending:
        if reason.get("text") not in [r.get("text") for r in _pending[cid]]:
            _pending[cid].append(reason)
    else:
        _pending[cid] = [reason]
        _order.append(cid)


def forget_events(ev_ids: set) -> int:
    """机主删掉了这些事件 → 把还排在队列里、由它们生出来的醒因一起撤掉，返回撤了几条。

    为什么必须有（2026-08-19 实录）：事件一落地，醒因的**文本拷贝**就进了 _pending
    等着执行，删事件流里的原件动不到这份内存副本——机主按了暂停、删了「摇了摇头，
    没让抱」，一按开始，那一轮的【这次发生了什么】里它照样在，AI 的心流里也就留下了
    一条本该不存在的记录。删记录要连队列里的影子一起删。

    撤不回的只剩一种：正在生成的那一轮（注入在执行开始时就组好了）——那个交给
    调用方报给机主看，别假装删干净了。"""
    if not ev_ids:
        return 0
    n = 0
    with _lock:
        for cid in list(_pending):
            keep = [r for r in _pending[cid] if r.get("ev") not in ev_ids]
            n += len(_pending[cid]) - len(keep)
            if keep:
                _pending[cid] = keep
            else:
                _pending.pop(cid, None)
                if cid in _order:
                    _order.remove(cid)
    if n:
        logerr(f"cohabit：机主删了事件，跟着撤掉 {n} 条还没执行的醒因")
    return n


def external_input() -> None:
    """有新外部输入（用户发言/动作/移动/状态编辑/手机消息）→ 连发计数全体清零。
    用户是这个世界唯一的外部输入源，粗粒度到"全体"就够了。"""
    with _lock:
        _syswake_run.clear()
        _gate_hit.clear()


def _may_chain(cid: str) -> bool:
    """move 执行前的连发判定（cohabit.do_cohabit_wake 的 chain_allowed 回调）：
    拦在瞬移之前——补醒注定入不了队的话，不该让人先移过去再哑掉。"""
    with _lock:
        return _syswake_run.get(cid, 0) < config.COHABIT_CHAIN_N


# ---------- 事件醒来（world.event_hook）----------
def _reason_text(room_name: str, ev: dict) -> str:
    name = world.entity_name(ev.get("actor", ""))
    t = ev.get("type")
    if t == "speech":
        return f"「{room_name}」里，{name}说：「{ev.get('text', '')}」"
    if t == "action":
        return f"「{room_name}」里，{name} *{ev.get('text', '')}*"
    return f"「{room_name}」里：{ev.get('text', '')}"   # system 事件文本自带人名


def _on_room_event(room_id: str, ev: dict) -> None:
    """world.append_event 的钩子。世界锁内被调：只做在场判定 + 入队，快进快出。
    系统通知与发言/动作先都触发（PLAN：观察 token 后再考虑给通知类降概率）。"""
    if not config.COHABIT_ENABLED:
        return
    actor = ev.get("actor")
    try:
        room_name = world.room(room_id).get("name", room_id)
    except KeyError:
        return
    # ev＝这条醒因是哪条事件生出来的：机主删掉那条事件时，还排在队列里的这份拷贝
    # 要跟着撤（forget_events）——不然删的是原件，队列里的副本照样被组装进注入。
    reason = {"kind": "event", "text": _reason_text(room_name, ev), "ev": ev.get("id")}
    # 作者排除 + 用户不是 AI + 宠物不走这条线（猫的醒来在 pet_queue，这个队列起的是
    # claude -p，把猫入队会在 characters.resolve 处炸）。
    targets = [e for e in world.occupants(room_id)
               if e != actor and e != world.USER_ID and not pets.is_pet(e)]
    random.shuffle(targets)   # 谁先接话随机：occupants 按注册序出，不洗的话 default 永远抢首
    pet_actor = pets.is_pet(actor)
    for e in targets:
        if pet_actor:
            # 猫的动静唤人（PLAN_pet P2）：①发起者排除——他刚用工具逗的猫，反应已在
            # 工具结果里同轮拿到，事件再唤他就是自唤醒回环；②概率降权——猫翻个身
            # 不值得每次都烧一次 claude -p。连发计数照常不重置（external_input 只有
            # 用户路由会调，猫够不着，结构性成立）。
            if e == pet_queue.initiator():
                continue
            if random.random() >= pet_queue.PET_TO_CHAR_PROB:
                continue
        enqueue(e, reason, system=True)
    # 猫这边的事件响应（概率+间隔+连发闸全在 pet_queue；世界锁内只入队不调引擎）
    pet_queue.on_room_event(room_id, ev)


def install() -> None:
    world.event_hook = _on_room_event


def uninstall() -> None:
    world.event_hook = None


# ---------- 手机消息附带的 move（聊天轮末，app.finalize_chat_reply 调）----------
def chat_move(char_id: Optional[str], target: str) -> Optional[dict]:
    """聊天回复带了 [[move:X]]：轮末执行移动，结果补醒入队（成功/失败同一条路）。
    开关关着 / 目的地不认识 → 当没写（标记反正已被剥掉，不影响聊天）。"""
    if not config.COHABIT_ENABLED:
        return None
    cid = characters.resolve(char_id)
    try:
        mv = world.move(cid, target)
    except KeyError:
        logerr(f"聊天 move 目的地不认识，当没写：{target[:40]!r}")
        return None
    if mv.get("noop"):
        return mv
    enqueue(cid, cohabit.move_result_reason(mv), system=True)
    logerr(f"聊天附带 move（{cid}）：→ {target}，ok={mv['ok']}，补醒已入队")
    return mv


def chat_move_hint(char_id: Optional[str]) -> str:
    """聊天 prompt 的可选提示（app._prepare_chat 注入）。开关关着返回空串。
    只报房间 id/名字，不带里面有谁——认知边界在聊天路同样从源头执行。"""
    if not config.COHABIT_ENABLED:
        return ""
    cid = characters.resolve(char_id)
    loc = world.location_of(cid)
    if loc == world.AWAY:
        where = "出门在外"
    else:
        where = world.room(loc).get("name", loc)
    rooms = "、".join(f"{rid}={r.get('name', rid)}"
                     for rid, r in world.load_registry().items() if rid != loc)
    return (f"【可选：你现在人在「{where}」。想挪个地方就在回复里带 [[move:房间id]]"
            f"（可选：{rooms}）——标记会被剥掉、对方看不到；移动在这轮结束后发生，"
            f"到了那边会再叫醒你一次。没必要就别写。】")


# ---------- 自主醒来（独处即可，任何地点）----------
def solo_wakes_today(cid: str) -> int:
    """今天的自主醒来次数（预算口径：只数 solo/scheduled，事件/move 补醒不占预算）。"""
    today = wake._date_str(int(time.time()))
    return sum(1 for w in state_store.read_wake_log(limit=1000, char_id=cid)
               if w.get("source") == "wake"
               and str(w.get("trigger", "")).startswith(("cohabit:solo", "cohabit:scheduled"))
               and wake._date_str(int(w.get("ts", 0))) == today)


def _alone(cid: str) -> bool:
    """独处 = 所在地点没有别人（任何地点；出门在外不算——char 出门本身还是后置功能）。
    猫不算「别人」：同屋趴着一只猫照样算独处，随机醒不该被一只猫压住（PLAN_pet P0）。"""
    snap = world.world_snapshot()
    loc = snap[cid]["location"]
    if loc == world.AWAY:
        return False
    return not any(e != cid and v["location"] == loc and not pets.is_pet(e)
                   for e, v in snap.items())


def _solo_check(cid: str, now: float) -> None:
    settings = state_store.load_settings(cid)
    if not settings.get("enabled", True):
        return
    if wake.chat_turn_active(cid):
        return
    sched = state_store.read_schedule(cid)
    if now < float(sched.get("cooldown_until") or 0):
        return
    if now - float(sched.get("last_wake_at") or 0) < wake.MIN_WAKE_GAP_SEC:
        return
    # 预算：只拦自主醒（事件/手机不占）。到点的 NEXT 也一样待命、日切兑现（老口径）。
    budget = settings.get("wake_daily_budget")
    if budget is not None and solo_wakes_today(cid) >= int(budget):
        return
    nw = sched.get("next_wake_at")
    if nw is not None and now >= float(nw):
        # 他自己定的点：到点必醒，不要求独处（「写了我保证到那个点把你醒一次」）。
        enqueue(cid, {"kind": "scheduled",
                      "text": "你之前给自己定了这个点醒来，现在到点了"}, system=False)
        return
    # 随机醒来的独立开关（2026-08-16 机主：他们自己会定 NEXT，随机的想关就关）。
    # 只关下面的概率掷骰——上面的 scheduled 到点、事件/手机唤醒全不受影响。
    # 判 `is False`：老客户端把设置存回来时该键是 None，不能算关。
    if settings.get("random_wake") is False:
        return
    if not _alone(cid):
        return
    # 用户刚说过话（手机）就别随机醒——打字打一半被抢话的体验，老 wake 同款闸。
    q = settings.get("quiet_after_user_min")
    if q:
        window = state_store.read_recent_window(cid)
        user_ts = next((int(m["ts"]) for m in reversed(window)
                        if m.get("role") == "user" and m.get("ts")), None)
        if user_ts is not None and now - user_ts < q * 60:
            return
    from datetime import datetime
    local_now = datetime.now(config.APP_TZ)
    freq = settings.get("day_freq" if wake.is_daytime(settings, local_now)
                        else "night_freq", "low")
    prob = wake.FREQ_PROB_15.get(freq, 0.08) * (SOLO_TICK_SEC / 900)
    if random.random() < prob:
        enqueue(cid, {"kind": "solo",
                      "text": "没什么特别的事，你就是自己醒了——这会儿周围没别人"},
                system=False)


def _solo_tick(now: Optional[float] = None) -> None:
    if not config.COHABIT_ENABLED:
        return
    # code 会话只拦**归属角色**的自主醒（他人在电脑前）：M2 消息已按角色分会话，
    # 老 wake「避让对所有角色」的挤同屏理由不再成立；别的角色照常过自己的日子。
    # 事件触发也只对归属角色延后（_pop_next），事实照常落盘。
    busy = _code_owner()
    now = now or time.time()
    for cid in characters.ids():
        if cid == busy:
            continue
        try:
            _solo_check(cid, now)
        except Exception as e:
            logerr(f"cohabit solo 判定出错（{cid}，忽略）: {e}")


# ---------- worker（执行锁并发 = 1）----------
def _pop_next() -> tuple[Optional[str], list[dict]]:
    if _paused["on"]:
        return None, []    # 暂停：谁都不弹，pending 原地攒着（合并去重照常）
    busy = _code_owner()   # 锁外探（可能起 tmux 子进程，别拿着队列锁等它）
    if busy and busy in _pending and not _defer_hit.get(busy):
        logerr(f"cohabit 延后（{busy}）：code/game 会话开着，醒来攒着等收工")
        _defer_hit[busy] = True
    with _lock:
        cid = next((c for c in _order
                    if not wake.chat_turn_active(c) and c != busy), None)
        if cid is None:
            return None, []
        _order.remove(cid)
        reasons = _pending.pop(cid)
        # 计数在执行开始时加，但**只数系统触发**（事件/move 补醒）——闸拦的是"别人在推他"
        # 的连锁，自主醒来不是任何链条的延续，反过来把计数断掉（同 external_input）。
        # 2026-08-17 的坑：solo/scheduled 也计数、又只有机主的外部输入能清零 → 机主一睡，
        # 四次自主醒来就把闸顶满，之后每一轮的 MOVE 都在瞬移前被拦，人被钉死在原地
        # （Cassius 连着五轮写了 MOVE 全被吞，日志只有一行 stderr）。
        if any(r.get("kind") in ("solo", "scheduled") for r in reasons):
            _syswake_run[cid] = 0
            _gate_hit.pop(cid, None)
        else:
            _syswake_run[cid] = _syswake_run.get(cid, 0) + 1
        return cid, reasons


def _drain() -> None:
    """把队列跑空。单线程调用 = 全局醒来并发 1；注入在 do_cohabit_wake 里组装
    （执行开始时），排队者天然看到最新世界。chat 轮进行中的角色跳过（pending 保留，
    下个信号/超时重试）。"""
    while True:
        cid, reasons = _pop_next()
        if cid is None:
            return
        try:
            # 全局执行锁跨两套系统（wake.WAKE_EXEC_LOCK）：邮件硬触发跑在老路的线程池，
            # 不共锁就可能和这里同时起两个 claude -p。
            with wake.WAKE_EXEC_LOCK:
                _executing["cid"] = cid
                try:
                    res = cohabit.do_cohabit_wake(cid, reasons,
                                                  chain_allowed=_may_chain,
                                                  on_move_result=lambda c, r: enqueue(c, r, system=True))
                finally:
                    _executing["cid"] = None
            if res.get("action") == "error":
                # 错误冷却 30 分钟（口径同 do_wake_sync）：enqueue/solo 两头都认这个字段。
                with state_store.SCHEDULE_LOCK:
                    sched = state_store.read_schedule(cid)
                    sched["cooldown_until"] = int(time.time()) + 1800
                    state_store.write_schedule(sched, cid)
        except wake.Overloaded as e:
            # 模型那头过载（已隔 30s 重试过一次）：**不压冷却**——不是我们坏了，压 30 分钟
            # 等于替对面的临时故障惩罚自己。改成按下暂停键 + 把错误摆到 UI 上，人来定夺
            # （2026-08-17 机主拍板）。原因塞回队列：按「开始」时这一轮照常兑现。
            logerr(f"cohabit 醒来放弃（{cid}）：{e}——队列已暂停，等机主定夺")
            with _lock:
                for r in reasons:
                    _push(cid, r)
            _last_error.update(text=f"{world.entity_name(cid)}没醒成：{e}", ts=int(time.time()))
            set_paused(True)
            return
        except Exception as e:
            logerr(f"cohabit 醒来执行出错（{cid}，丢弃本次）: {e}")


def worker_loop() -> None:
    """常驻 worker（daemon 线程，app lifespan 里起）。信号驱动 + 超时兜底：
    事件入队 set 一下就动，没事时每 _WORKER_WAIT_SEC 醒一次做 solo 判定。"""
    logerr(f"cohabit worker 启动（连发上限 N={config.COHABIT_CHAIN_N}，"
           f"solo tick={SOLO_TICK_SEC}s）")
    last_solo = 0.0
    while True:
        _signal.wait(timeout=_WORKER_WAIT_SEC)
        _signal.clear()
        try:
            now = time.time()
            offers.sweep(now)   # 抱人邀约的超时/失效扫除（发起人收作废补醒）
            if now - last_solo >= SOLO_TICK_SEC:
                last_solo = now
                _solo_tick(now)
            _drain()
        except Exception as e:
            logerr(f"cohabit worker 出错（忽略，继续）: {e}")
