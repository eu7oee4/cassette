"""同居世界的醒来统一模型 + ACTION 协议（PLAN_cohabit C1）。

醒来只有一种：注入一样，区别只在触发原因（可多条）。本模块负责：
① 注入组装（世界观三条 / 我在哪 / 这里有谁 / 地点状态 / 在场事件 / 手机线程+内心 / 原因）；
② ACTION 协议解析——组合规则**在解析处结构性执行**，不靠 prompt 纪律：
   每轮至多一个表达（act / phone 二选一，互斥字段直接丢弃）+ 至多一个 move（末位执行）；
③ 动作落地（world.act / wake.try_push / world.move）；
④ move 必有一轮结果补醒（成功/失败同一条路，本模块内直接串链，深度硬停 N_CHAIN——
   C2 的队列化连发上限会取代它，这里只是不带队列时的安全绳）。

**本模块此刻不接任何触发**：怎么被叫醒（事件/独处/手机）是 C2 的事。现有 wake.py
的聊天世界醒来一个字没动，两条路并存到 C4 切换。

世界观注入（2026-08-16 拍板「加土别加形状」）：只写三条机制——有位置 / 认知边界 /
别人是真的；关系、感受、该怎么相处一概不写，留给他自己长。落在这里的提示词模板，
不碰任何 persona。
"""
import re
import time
import uuid
from typing import Optional

import characters
import code_bridge
import config
import pipeline
import plugins
import state_store
import wake
import world
from notify import logerr

# move 补醒链的递归硬停（含首轮）。C2 的连发上限（入队处拦，参数 N=4）落地后，
# 队列保证不会串这么深，这根安全绳只防 C1 独跑时模型每轮都 move 的死循环。
N_CHAIN = 4

# 每轮 act 可附带的状态改动条数上限（口径在 world.MAX_STATE_OPS_PER_ACT，解析处执行）。
_STATE_ADD_RE = re.compile(r"^\s*add\s*[:：]\s*(.+?)\s*$", re.I)
_STATE_EDIT_RE = re.compile(r"^\s*edit\s+([0-9a-f]{8})\s*[:：]\s*(.+?)\s*$", re.I)
_STATE_REMOVE_RE = re.compile(r"^\s*remove\s+([0-9a-f]{8})\s*$", re.I)

_WORLDVIEW = """【这个世界怎么运作——三条，都是机制，记住就好】
1. 你有位置了。你此刻真实地待在下面写的那个地方，房间有门、可能有锁，别人能走进来，你也能走出去。这不是比喻。
2. 你只知道你所在地点发生的事。别处此刻发生着什么、别人不在你眼前时在做什么，你不知道——不知道就是不知道，不要推测补全。
3. 这里的其他住户是真的。他们和你一样会醒来、会移动、会自己决定说不说话。他们不是布景，也不归你演。"""


# ---------- 电脑前状态（同居世界里 code/game 会话的物理语义）----------
def coding_char():
    """电脑前的那个人：code/game 会话活着 → (会话归属角色, "code"|"game")，没有 → None。
    这是同居世界对「他在 code 会话里」的物理口径：人还在原房间，只是**在电脑前专注着**
    ——位置不变、状态可见。用途：①注入里给在场者标注（减少被打扰）；②队列对归属
    角色的醒来延后（cohabit_queue._pop_next）；③ /world 给 UI 出状态。
    探测失败当没有（口径同 wake.code_session_open：宁可醒、别静默困死）。"""
    try:
        if not code_bridge.session_alive():
            return None
        owner = code_bridge.session_char() or plugins.owner_of("tmux")
        if not owner:
            return None
        return owner, ("game" if code_bridge.active_profile() == "game" else "code")
    except Exception as e:
        logerr(f"cohabit 探 code 会话失败（当作没开）: {e}")
        return None


def _busy_label(prof: str) -> str:
    return "玩游戏" if prof == "game" else "敲代码"


# ---------- 注入组装 ----------
def _render_event(ev: dict, self_id: str) -> str:
    name = "你" if ev.get("actor") == self_id else world.entity_name(ev.get("actor", ""))
    t = pipeline.fmt_ts(int(ev.get("ts", 0)))
    text = ev.get("text", "")
    if ev.get("type") == "action":
        return f"[{t}] {name} *{text}*"
    if ev.get("type") == "speech":
        return f"[{t}] {name}：「{text}」"
    return f"[{t}] （{text}）"   # system：进出 / 状态改动，事件文本自带人名


def _where_block(cid: str, ev_limit: int = 80) -> str:
    """「你在哪 + 这里有谁 + 地点状态 + 你在场看到的」。走廊/出门也如实说——
    那两处不是房间，没有事件流，能做的只有 move 和 phone。"""
    loc = world.location_of(cid)
    if loc == world.AWAY:
        return "【你在哪】你出门在外，不在房子里。房子里的事你看不见；手机随时可用。"
    if loc == world.HALLWAY:
        return "【你在哪】你站在走廊里，不在任何房间。想进哪个房间就 MOVE 过去。"
    r = world.room(loc)
    busy = coding_char()
    others = []
    for e in world.occupants(loc):
        if e == cid:
            continue
        name = world.entity_name(e)
        if busy and e == busy[0]:
            # 在场者状态：正在电脑前的人标出来——「别打扰」是状态事实（土），
            # 打不打扰他自己决定（不替他写形状）。
            name += f"（正在电脑前{_busy_label(busy[1])}，看起来很专注——先别打扰，有话可以留到他忙完）"
        others.append(name)
    who = f"这里还有：{('、'.join(others))}。" if others else "现在这里只有你一个人。"

    now = int(time.time())
    state = world.room_state(loc)
    if state:
        lines = [f"- [{s['id']}] {s['text']}（{world.entity_name(s.get('author', ''))}，"
                 f"{pipeline.fmt_gap(now - int(s.get('since', now)))}前）" for s in state]
        state_block = "【这里现在有什么——快照不是日志，收拾掉就 remove，别追加叙述】\n" + "\n".join(lines)
        if len(state) > world.ROOM_STATE_SOFT_CAP:
            state_block += f"\n（条目有点多了，过时的顺手清一清）"
    else:
        state_block = "【这里现在有什么】（没什么特别的）"

    if not ev_limit:   # 事件另有去处（聊天路：经历流已并进时间线），只要现场快照
        return f"【你在哪】你在「{r['name']}」。{who}\n\n{state_block}"
    evs = world.visible_events(loc, cid, limit=ev_limit)
    ev_block = ("【你在这个房间看到的（从你这次进来算起）】\n" +
                "\n".join(_render_event(e, cid) for e in evs)) if evs else \
        "【你在这个房间看到的】（你在这儿的这段时间还没发生什么）"
    return f"【你在哪】你在「{r['name']}」。{who}\n\n{state_block}\n\n{ev_block}"


def _rooms_block(cid: str) -> str:
    """MOVE 的目的地清单：只有 id / 名字 / 楼层——**不带里面有谁**（认知边界是机制，
    不是嘱咐：别处的事从注入源头就不给）。"""
    loc = world.location_of(cid)
    lines = [f"- {rid}（{r.get('floor', '?')}层）：{r.get('name', rid)}"
             for rid, r in world.load_registry().items() if rid != loc]
    return "【你可以去的地方（MOVE 用房间 id）】\n" + "\n".join(lines) + \
        "\n（锁着的门会进不去——试了才知道。）"


def cohabit_prompt(cid: str, reasons: list[dict], settings: dict) -> str:
    """统一醒来的注入。persona 走 base_claude_args 的 --system-prompt-file，不在这里。"""
    u = config.user_name()
    reason_lines = "\n".join(f"- {r.get('text', '')}" for r in reasons if r.get("text"))

    window = state_store.read_recent_window(cid)
    wake_n = min(max(int(settings.get("wake_window_n") or 50), 20), 300)
    timeline = pipeline.build_context_timeline(window[-wake_n:], char_id=cid) or "（最近没有对话）"

    menu = pipeline.tool_menu_block("wake", cid)
    menu_section = f"\n{menu}\n" if menu else ""

    # 手机这轮会被打扰控制拦 → 先说清再让他选（闸只拦推送不拦思考，但他有权知道）。
    blocked = wake.push_block(settings, cid)
    blocked_section = f"\n{blocked[1]}\n" if blocked else ""

    # 诚实兜底：归属角色的醒来正常被队列延后到收工（cohabit_queue._pop_next），
    # 这段按理永不出现。但判据写「会话开着就注入」不写「不可能」——万一哪条路绕过
    # 避让，prompt 也不说谎（口径同 wake.code_session_block 的设计注释）。
    code_section = ""
    busy = coding_char()
    if busy and busy[0] == cid:
        code_section = (f"\n【注意：你此刻还开着电脑上的会话在{_busy_label(busy[1])}——"
                        f"按避让规则这次醒来本该等你收工，出现这段说明有触发绕了过来。"
                        f"这轮你手上没有那边的工具；说话做事别跟那边正在干的活打架。】\n")

    return f"""【这是一次你自己的醒来，不是{u}发来的消息】
现在是 {pipeline.now_str()}。
{pipeline.pronoun_hint()}
{_WORLDVIEW}

{_where_block(cid)}

{_rooms_block(cid)}

【你和{u}的手机线程 + 你自己醒来时的内心，按时间顺序——这是手机，跟房间里说话是两回事】
{timeline}
{menu_section}{blocked_section}{code_section}
【这次为什么醒】
{reason_lines or '- （无特别原因，就是醒了）'}

想清楚这轮要不要做点什么：在房间里动作/说话（act）、给{u}的手机发消息（phone）、挪个地方（MOVE）、或者什么都不做（none）。没话可说就安静待着，不用硬找话。
严格按下面格式回答（每个标签一行开头，英文+冒号，全部都要写，用不上的留空）：
THOUGHTS: <你此刻真实的内心，几句话>
ACTION: <none / act / phone，三选一。act=在你所在的房间里表达；phone=给{u}手机发消息，人在哪都行；一轮只能选一样>
MOTION: <ACTION=act 时你做的动作，第三人称白描（别带星号），如「把杯子放回桌上」；没有留空>
SAY: <ACTION=act 时你在房间里说的话。可以夹 *动作*（星号包起来），会按顺序拆成 动作/说话/动作/说话 分开上屏；不说留空>
STATE: <ACTION=act 时顺手改这里的地点状态，每行一条、最多 {world.MAX_STATE_OPS_PER_ACT} 条：add: 文本 ／ edit 条目id: 新文本 ／ remove 条目id。⚠️ 快照写的是**这里的东西和环境**（桌上剩了半杯牛奶、窗帘拉开了），你的身体姿势不进快照——你在干嘛用 MOTION/SAY 表达；不改留空>
PHONE: <ACTION=phone 时发给{u}的消息>
MOVE: <想去哪就写上面清单里的房间 id；id 后可空格接一句进场的样子，如 "living_room 打着哈欠晃进来"；不动写 "无"。移动发生在这一轮的最后，走完下一轮会告诉你结果>
CARRY: <配合 MOVE：想抱着{u}一起走就写「{u}」（前提是{u}此刻和你同屋）；不带人写 "无">
NEXT: <你希望多久后再自主醒来，如 "90分钟" 或 "3小时"；没想法写 "无">
"""


def house_context_for_chat(char_id) -> str:
    """手机聊天注入的小屋现场快照（你在哪/这里有谁/地点状态）。
    房间事件**原文不在这儿**——经历流已按时间并进聊天时间线
    （build_context_timeline 的 experience_limit 路，2026-08-16 机主拍板 B 案），
    这里再摆一遍就是重复注入，只留现场快照 + 通道框定。开关关着返回空串。"""
    if not config.COHABIT_ENABLED:
        return ""
    cid = characters.resolve(char_id)
    return ("【小屋现场——你此刻真实待在这里；你在屋里看见过的对话和动作"
            "已按时间排进下面的时间线（带（房间名）前缀的就是）】\n"
            + _where_block(cid, ev_limit=0)
            + "\n【注意通道：现在这条消息是手机上来的，你回的也是手机短信，"
              "不是在房间里开口说话。】")


# ---------- ACTION 协议解析（组合规则在这里结构性执行）----------
_LABELS = ["THOUGHTS", "ACTION", "MOTION", "SAY", "STATE", "PHONE", "MOVE", "CARRY", "NEXT"]


def _sections(text: str) -> dict:
    idx = {}
    for lb in _LABELS:
        m = re.search(rf"(?im)^\s*{lb}\s*[:：]", text)
        if m:
            idx[lb] = (m.start(), m.end())
    out = {}
    for lb in _LABELS:
        if lb not in idx:
            out[lb] = ""
            continue
        start = idx[lb][1]
        laters = [idx[o][0] for o in _LABELS if o in idx and idx[o][0] > idx[lb][0]]
        out[lb] = text[start:min(laters) if laters else len(text)].strip()
    return out


def parse_cohabit_output(text: str) -> dict:
    """切协议段并执行组合规则。返回：
    {thoughts, action: none|act|phone, motion, say, state_ops: [(op, id, text)],
     phone, move: room_id|None, next_min, next_raw}
    规则全在结构上：act/phone 互斥（选谁丢谁的字段）、STATE 超上限截断、
    MOVE 目的地必须是注册房间（不是就当没写）。"""
    s = _sections(text)
    action_raw = s["ACTION"].lower()
    action = "act" if "act" in action_raw else ("phone" if "phone" in action_raw else "none")

    motion = say = phone = ""
    state_ops: list[tuple] = []
    if action == "act":
        motion = pipeline.strip_markers(s["MOTION"]).strip().strip("*").strip()
        say = pipeline.strip_markers(s["SAY"]).strip()
        for ln in s["STATE"].splitlines():
            ln = ln.strip()
            if not ln or ln in ("无", "none"):
                continue
            if (m := _STATE_ADD_RE.match(ln)):
                state_ops.append(("add", None, m.group(1)))
            elif (m := _STATE_EDIT_RE.match(ln)):
                state_ops.append(("edit", m.group(1), m.group(2)))
            elif (m := _STATE_REMOVE_RE.match(ln)):
                state_ops.append(("remove", m.group(1), None))
            else:
                logerr(f"cohabit STATE 行不认识，丢弃：{ln[:60]!r}")
        if len(state_ops) > world.MAX_STATE_OPS_PER_ACT:
            logerr(f"cohabit STATE 超上限（{len(state_ops)}>{world.MAX_STATE_OPS_PER_ACT}），截断")
            state_ops = state_ops[:world.MAX_STATE_OPS_PER_ACT]
        if not motion and not say and not state_ops:
            action = "none"   # act 但什么都没写 → 兜底成 none
        if s["PHONE"].strip():
            logerr("cohabit：ACTION=act 但 PHONE 有内容，按互斥规则丢弃")
    elif action == "phone":
        phone = s["PHONE"].strip()
        if not phone:
            action = "none"
        if s["MOTION"].strip() or s["SAY"].strip() or s["STATE"].strip():
            logerr("cohabit：ACTION=phone 但带了 act 字段，按互斥规则丢弃")

    # MOVE：`房间id` 或 `房间id 进场的样子`（空格后接一句，如 "living_room 打着哈欠晃进来"）。
    move_raw = s["MOVE"].strip().strip("「」\"'` ")
    move: Optional[str] = None
    move_motion = ""
    if move_raw and move_raw not in ("无", "none", "不动"):
        head, _, rest = move_raw.partition(" ")
        head = head.strip()
        if head in world.load_registry():
            move = head
            move_motion = pipeline.strip_markers(rest).strip().strip("*").strip()
        else:
            logerr(f"cohabit MOVE 目的地不认识，当没写：{move_raw[:40]!r}")

    carry_raw = s["CARRY"].strip().strip("「」\"'` ")
    if carry_raw in ("无", "none", ""):
        carry_raw = ""

    next_min = pipeline.parse_next_minutes(s["NEXT"])
    return {"thoughts": s["THOUGHTS"], "action": action, "motion": motion, "say": say,
            "state_ops": state_ops, "phone": phone, "move": move, "move_motion": move_motion,
            "carry_raw": carry_raw, "next_min": next_min, "next_raw": s["NEXT"].strip()}


# ---------- 执行 ----------
def _run(prompt: str, cid: str):
    """起模型（独立函数便于测试替身）。老规矩全在 run_claude_wake：context='wake'
    的插件过滤、Ombre 挂载、超时与出错口径。"""
    return wake.run_claude_wake(prompt, char_id=cid)


def move_result_reason(mv: dict) -> dict:
    """move 结果 → 补醒原因（成功/失败同一条路，只有文本不同）。
    队列（C2）和模块内递归共用，聊天附带的 move 也走它。"""
    if mv["ok"]:
        carried = f"（你是抱着{world.entity_name(mv['carry'])}过来的，人还在你怀里）" \
            if mv.get("carry") else ""
        return {"kind": "move_result",
                "text": f"你刚到「{world.room(mv['to'])['name']}」{carried}——下面【你在哪】"
                        f"就是这里现在的样子。想打个招呼、做点什么，这轮就是给你的。"}
    return {"kind": "move_result",
            "text": f"{mv.get('text', '门锁着，没进去')}——你还在原地。"
                    f"改道去别处、掏手机、或者就算了，都行。"}


def do_cohabit_wake(cid: str, reasons: list[dict], _chain_no: int = 1,
                    chain_allowed=None, on_move_result=None) -> dict:
    """一次统一醒来：组注入 → 起模型 → 解析 → 落地 → （若 move）结果补醒。
    返回最后一轮的结果 dict（含 chain 长度，方便测试/日志断言）。

    补醒链两种走法：
    - 默认（两个回调都 None）：模块内直接递归，深度硬停 N_CHAIN——C1 独跑的安全绳；
    - 队列驱动（C2）：chain_allowed(cid) 在 move 执行**前**判连发上限（拦得住就不该
      让人先瞬移再哑掉），on_move_result(cid, reason) 把补醒入队后本轮即返回。"""
    cid = characters.resolve(cid)
    settings = state_store.load_settings(cid)
    now_ts = int(time.time())
    trigger = "cohabit:" + ",".join(r.get("kind", "?") for r in reasons)

    raw, stored = _run(cohabit_prompt(cid, reasons, settings), cid)
    stored = [s for s in (stored or []) if s.get("tool") not in pipeline.NON_MEMORY_TOOLS]
    if raw is None:
        state_store.append_wake_log({"ts": now_ts, "time": pipeline.now_str(),
                                     "source": "wake", "action": "error",
                                     "trigger": trigger}, char_id=cid)
        return {"action": "error", "chain": _chain_no}

    p = parse_cohabit_output(raw)
    next_wake_at = (now_ts + p["next_min"] * 60) if p["next_min"] else None
    result = {**p, "trigger": trigger, "chain": _chain_no}

    # ① 表达（act / phone 二选一，解析处已互斥）
    if p["action"] == "act":
        loc = world.location_of(cid)
        entry = {"ts": now_ts, "time": pipeline.now_str(), "source": "wake",
                 "action": "act", "trigger": trigger, "thoughts": p["thoughts"],
                 "room": loc}
        if loc in (world.AWAY, world.HALLWAY):
            # 结构上到不了这儿的才怪：模型在走廊硬要 act。不落事件，如实记日志。
            logerr(f"cohabit：{cid} 在 {loc} 试图 act，无房间可写，丢弃")
            entry["action"] = "none"
            entry["note"] = "act_no_room"
        else:
            try:
                if p["motion"] or p["say"]:
                    # 混写落地：MOTION 作首段动作，SAY 里的 *…* 按序拆成动作/说话交错事件
                    mixed = (f"*{p['motion']}* " if p["motion"] else "") + p["say"]
                    world.act_mixed(cid, loc, mixed)
                    entry.update(motion=p["motion"], say=p["say"])
                applied = []
                for op, eid, text in p["state_ops"]:
                    try:
                        world.state_change(cid, loc, op, entry_id=eid, text=text)
                        applied.append(f"{op}:{(text or eid or '')[:40]}")
                    except (KeyError, ValueError) as e:
                        logerr(f"cohabit 状态改动失败（跳过这条）: {e}")
                if applied:
                    entry["state_ops"] = applied
            except world.NotPresent as e:
                logerr(f"cohabit act 被物理引擎拒绝: {e}")
                entry["note"] = "not_present"
        if stored:
            entry["stored"] = stored
        if next_wake_at:
            entry["next_wake_at"] = next_wake_at
            entry["next_wake_note"] = pipeline.next_wake_note(p["next_raw"], next_wake_at)
        state_store.append_wake_log(entry, char_id=cid)
    elif p["action"] == "phone":
        # 手机走现有推送线：表情标记、打扰控制、outbox、Bark、窗口、日志全在 try_push。
        app_text, sticker_ids, bark_text = pipeline.split_wake_stickers(
            p["phone"], pipeline.sticker_handle_map(state_store.read_sticker_catalog()))
        if not app_text and not sticker_ids:
            app_text = bark_text = p["phone"]
        app_text = pipeline.strip_markers(app_text).strip()
        result["pushed"] = wake.try_push(
            app_text, settings, p["thoughts"], trigger,
            sticker_ids=sticker_ids, bark_text=bark_text,
            next_wake_at=next_wake_at,
            next_wake_note=pipeline.next_wake_note(p["next_raw"], next_wake_at)
            if next_wake_at else "",
            started_ts=now_ts, stored=stored, char_id=cid)
    else:
        entry = {"ts": now_ts, "time": pipeline.now_str(), "source": "wake",
                 "action": "none", "trigger": trigger, "thoughts": p["thoughts"]}
        if stored:
            entry["stored"] = stored
        if next_wake_at:
            entry["next_wake_at"] = next_wake_at
            entry["next_wake_note"] = pipeline.next_wake_note(p["next_raw"], next_wake_at)
        state_store.append_wake_log(entry, char_id=cid)

    # ② NEXT 落调度（口径同 do_wake_sync：明确定了新点用新的；scheduled 触发只消费已过期的点）
    with state_store.SCHEDULE_LOCK:
        sched = state_store.read_schedule(cid)
        sched["last_wake_at"] = now_ts
        cur_next = sched.get("next_wake_at")
        if next_wake_at is not None:
            sched["next_wake_at"] = next_wake_at
        elif any(r.get("kind") == "scheduled" for r in reasons) \
                and (cur_next is None or float(cur_next) <= now_ts):
            sched["next_wake_at"] = None
        state_store.write_schedule(sched, cid)

    # ③ move（末位执行）+ 结果补醒：成功/失败同一条路，只是原因文本不同。
    if p["move"]:
        allowed = chain_allowed(cid) if chain_allowed is not None else _chain_no < N_CHAIN
        if not allowed:
            # 表达照常落地了，只有 move 被停——写日志，别静默吞（排查"他为什么没走成"用）。
            logerr(f"cohabit：{cid} 连锁醒来达到上限，MOVE 不执行")
            result["move_stopped"] = True
            return result
        # CARRY 解析（政策层）：只认**此刻同屋的用户**——名字或 id 都行；对不上就当
        # 没写并记日志（隔空抱人/抱别的 AI 在结构上不存在，团团入住后再放开到猫）。
        carry = None
        if p["carry_raw"]:
            loc_now = world.location_of(cid)
            u_name = world.entity_name(world.USER_ID)
            if p["carry_raw"] in (world.USER_ID, u_name) \
                    and world.location_of(world.USER_ID) == loc_now:
                carry = world.USER_ID
            else:
                logerr(f"cohabit CARRY 抱不了（不同屋/不认识/非用户），当没写："
                       f"{p['carry_raw']!r}")
        try:
            mv = world.move(cid, p["move"], carry=carry)
        except ValueError as e:
            # 预检和落地之间世界可能变了（她刚好走开）：退成独自移动，别整轮报废。
            logerr(f"cohabit CARRY 落地时失效（{e}），改为独自移动")
            mv = world.move(cid, p["move"])
        result["move_result"] = mv
        if mv.get("noop"):
            return result
        # 进场自带的样子（MOVE 第二段）：enter 事件之后紧跟一条动作——
        # 「Cassius 进来了」+「*打着哈欠晃进来*」，在场的人一并看到。
        if mv["ok"] and p.get("move_motion"):
            try:
                world.act(cid, mv["to"], action=p["move_motion"])
            except Exception as e:
                logerr(f"cohabit 进场动作没写上（忽略）: {e}")
        reason = move_result_reason(mv)
        if on_move_result is not None:
            on_move_result(cid, reason)   # 队列驱动：补醒入队（同受单 pending 约束），本轮结束
            return result
        chained = do_cohabit_wake(cid, [reason], _chain_no=_chain_no + 1)
        chained["first_move"] = mv
        return chained
    return result
