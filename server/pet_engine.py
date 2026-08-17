"""猫引擎（PLAN_pet P1，从 mianmian pet_engine 搬家改造）：迷你醒来 + 直接互动。

一次 interact/wake：读状态 → 组注入 → 调 DeepSeek（OpenAI 兼容，JSON 强制）→
解析迷你协议 → 确定性覆盖 → 落地（房间事件 / 状态 / 姿态快照 / 移动）→ 记 petlog。

信息通道的结构性封堵（PLAN_pet 拍板，两道都在这里执行）：
- 注入只有：状态/需求/**当前房间**（在场者+自己可见的事件）/petlog/这次的触发——
  别处的对话根本不进上下文，猫带不走就转述不了；
- 协议**没有说话字段**：reply 只是 *动作白描* + 猫叫，猫物理上说不出人话。

对 mianmian 版的改造点：
- 猫有位置了：注入带房间上下文，协议多 move（猫洞：门禁不拦）和 pose（姿态进
  地点状态快照，引擎维护、猫走前自己清——「床不是地点」的落地）；
- poke 摘除：想找人自己 move 过去挠（needs 驱动，P2 接 tick）；
- 拉屎是需求不是提醒：压力值在 pet_store，这里做确定性覆盖（只在猫房生效、
  砂盆满了拒绝）和硬上限强制（enforce——生存性行为不靠模型自觉）。

**本模块不接任何触发**（P2 的猫 tick）也不挂工具（P3 的 pet MCP）：
interact()/wake()/enforce() 是给它们调的引擎面。跑在自己的线程里时不占
wake.WAKE_EXEC_LOCK——那是 claude -p 的锁，DeepSeek ~3.5s 一次，不抢。
"""
import json
import os
import re
import threading
import urllib.request
from typing import Optional

import pets
import pet_store
import world
from notify import logerr

# interact（工具线程）与 tick（pet worker）可能同时摸猫：引擎级串行，
# 免得两次模型调用交错着改同一份状态。猫只有一只，全局锁足够。
_ENGINE_LOCK = threading.Lock()

PETLOG_INJECT_N = 20     # 注入 petlog 条数（口径同 mianmian）
ROOM_EVENTS_N = 12       # 注入当前房间可见事件条数
PET_MODEL_TIMEOUT = int(os.environ.get("PET_MODEL_TIMEOUT", "60"))

# 固定反应动画标签（渲染层认这个集合；拉屎另有确定性覆盖）。
ACTION_TAGS = ["舔毛", "伸懒腰", "拉屎", "打滚"]

# 投喂/自助口径（注入模型；参考数值只作口径不硬编码，同 mianmian BUTTON_SPEC）。
FEED_SPEC = """【投喂口径】
- 罐罐：吃到饱自己会停，不挑但不贪嘴（饱腹≤30→吃完+40；≤70→填到70-80；更饱→少吃/不吃）。
- 冻干：饱腹+10（可加满无上限），心情+10（心情最多到70）。
- 猫条：饱腹+5（可加满无上限），心情+10（心情最多到70）。
【自助（都在猫房）】
- 自动喂食器：猫粮管够，但你挑食——饿了才吃、不饿不吃、心情不涨（饱腹≤50→填到50-60；≤60→填到60-65；>60→不吃）。
- 饮水机：一直有水，但你不爱喝水——60以下容易涨、60以上难涨，最多到70。
【拉屎口径】
- 只在猫房的猫砂盆解决。砂盆满了(3)你嫌脏不用——憋着，去挠人让他们铲。
【被撸口径】
- 摸头/摸下巴/挠下巴一般+心情；rua肚子/摸尾巴可能炸毛→心情-；玩耍+心情-精力。
- 睡/醒你自己说了算：困了睡、被吵到可以醒也可以装死继续睡（输出的 sleep 字段）。"""


class PetNotHere(Exception):
    """互动要求同地点——不在场就摸不着猫（API/工具层转 409）。"""


# ---------- 接线 ----------
def _engine_conf(pid: str) -> dict:
    return pets._conf(pid).get("engine") or {}


def _persona(pid: str) -> str:
    p = pets.PETS_DIR / pid / "persona.md"
    return p.read_text("utf-8") if p.exists() else ""


def _call_model(pid: str, prompt: str) -> str:
    """调 OpenAI 兼容 API（默认 DeepSeek）：persona 当 system、正文当 user，强制 JSON。
    接线优先 pet.json 的 engine 段，缺省回落 env（DEEPSEEK_*，口径同 mianmian）。"""
    conf = _engine_conf(pid)
    key = os.environ.get(conf.get("api_key_env") or "DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("未配置宠物引擎 API key（pet.json engine.api_key_env / DEEPSEEK_API_KEY）")
    url = conf.get("url") or os.environ.get(
        "DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")
    model = conf.get("model") or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": _persona(pid)},
                     {"role": "user", "content": prompt}],
        "temperature": 1.0,
        "max_tokens": 500,
        "response_format": {"type": "json_object"},
        # v4 系列默认带思考——猫要的是快速反应，关掉省时省钱（mianmian 同款）。
        "thinking": {"type": "disabled"},
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=PET_MODEL_TIMEOUT) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"]


# ---------- 注入 ----------
def _render_event(ev: dict, pid: str) -> str:
    name = "你" if ev.get("actor") == pid else world.entity_name(ev.get("actor", ""))
    if ev.get("type") == "action":
        return f"{name} *{ev.get('text', '')}*"
    if ev.get("type") == "speech":
        return f"{name}：「{ev.get('text', '')}」"
    return f"（{ev.get('text', '')}）"


def _room_block(pid: str) -> str:
    loc = world.location_of(pid)
    r = world.room(loc)
    others = [world.entity_name(e) for e in world.occupants(loc) if e != pid]
    who = f"这里还有：{('、'.join(others))}。" if others else "现在这里只有你。"
    evs = world.visible_events(loc, pid, limit=ROOM_EVENTS_N)
    ev_block = ("你刚才看到/听到的：\n" +
                "\n".join(f"- {_render_event(e, pid)}" for e in evs)) if evs else ""
    return f"你在「{r.get('name', loc)}」。{who}\n{ev_block}".rstrip()


def _rooms_block(pid: str) -> str:
    loc = world.location_of(pid)
    lines = [f"- {rid}：{r.get('name', rid)}"
             for rid, r in world.load_registry().items() if rid != loc]
    return "\n".join(lines)


def _build_prompt(pid: str, state: dict, trigger_lines: list[str]) -> str:
    stat = (f"饱腹 {state['satiety']} / 水分 {state['hydration']} / "
            f"精力 {state['energy']} / 心情 {state['mood']}")
    litter = f"{state['litter']} {pet_store.LITTER_CN.get(state['litter'], '')}"
    sleep = "睡着" if state.get("asleep") else "醒着"
    nd = pet_store.needs(state)
    needs_block = "\n".join(f"- {n['text']}" for n in nd) if nd else "- （现在挺舒坦，没什么想要的）"
    log = pet_store.read_petlog(pid, limit=PETLOG_INJECT_N)
    log_block = "\n".join(f"- {r.get('text', '')}" for r in log) if log else "（还没有记录）"
    trig = "\n".join(f"- {t}" for t in trigger_lines if t) or "- （没什么特别的）"

    return f"""【你现在的状态】
{stat}（养到第 {state.get('day', 1)} 天）
猫砂盆：{litter}；你{sleep}。

【你身体的感受】
{needs_block}

{FEED_SPEC}

【你在哪、谁在、你看到了什么——你只知道这个房间的事】
{_room_block(pid)}

【最近的照料记录（旧→新）】
{log_block}

【这次发生了什么】
{trig}

【你可以去的地方（move 用房间 id；门锁不锁都拦不住你）】
{_rooms_block(pid)}

【怎么回复——严格只输出一个 JSON，别加别的字】
{{
  "reply": "*动作白描* 加一声猫叫，给在场的人看的；不想理会就写空串",
  "action": "{' | '.join(ACTION_TAGS)} | none",
  "stats": {{"satiety": 数, "hydration": 数, "energy": 数, "mood": 数}},
  "sleep": true/false/null,
  "move": "想去哪写上面清单里的房间 id，不动写 null",
  "pose": "要留在这个房间地点状态里的姿态一句（如「蜷在沙发扶手上打盹」），不留写空串",
  "log": "记进照料日志的一句（你干了啥，简短）"
}}
规则：stats 是四条的**新绝对值**(0-100)，按口径你自己判断；没被影响的维持原值。
你是猫：reply 里只有动作和猫叫（喵/咕噜/嘶——），你说不出人话，也听不懂复杂的话，
只对语气和熟悉的词有反应。拉屎只在猫房猫砂盆有效；砂盆满了你不会用。
睡着时被戳可以继续装死（reply 空串、全部 null）。"""


# ---------- 解析 + 落地 ----------
def _parse(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # 抠不出 JSON：当没反应（猫不背模型的锅），原文进日志方便排查
    logerr(f"pet_engine 解析失败，当没反应：{text[:80]!r}")
    return {}


def _sanitize_stats(stats) -> Optional[dict]:
    if not isinstance(stats, dict):
        return None
    out = {k: stats[k] for k in pet_store.STAT_KEYS
           if isinstance(stats.get(k), (int, float))}
    return out or None


def _clear_pose(pid: str) -> None:
    """清姿态快照（猫走前自己清——锚点房间恒等于猫的当前房间，见 set_pose）。
    条目可能被别人先收了：清不掉就算了，锚点照样丢。"""
    anchor = pet_store._load_raw(pid).get("pose")
    if not anchor:
        return
    try:
        world.state_change(pid, anchor["room"], "remove", entry_id=anchor["entry_id"])
    except Exception:
        pass
    pet_store.set_pose_anchor(pid, None)


def _set_pose(pid: str, text: str) -> None:
    """姿态进当前房间的地点状态快照（「团团蜷在沙发扶手上打盹」——床不是地点，
    姿态才是）。已有锚点就 edit，没有就 add；失败只记日志不反噬这轮。"""
    loc = world.location_of(pid)
    anchor = pet_store._load_raw(pid).get("pose")
    text = f"{world.entity_name(pid)}{text}" if not text.startswith(world.entity_name(pid)) else text
    try:
        if anchor and anchor.get("room") == loc:
            world.state_change(pid, loc, "edit", entry_id=anchor["entry_id"], text=text)
        else:
            r = world.state_change(pid, loc, "add", text=text)
            pet_store.set_pose_anchor(pid, {"room": loc, "entry_id": r["entry"]["id"]})
    except Exception as e:
        logerr(f"pet_engine 姿态快照没写上（忽略）: {e}")


def _cat_room(pid: str) -> Optional[str]:
    rid = f"{pid}_room"
    return rid if rid in world.load_registry() else None


def _apply(pid: str, out: dict) -> dict:
    """协议落地（interact/wake 共用）：reply 事件 → 确定性覆盖 → 状态 → 姿态/移动。
    顺序讲究：reply 落在被逗的房间（move 末位执行，口径同 cohabit）；
    移动前清旧姿态（人走了就够不着旧房间的快照了）。
    整段套 world.turn()：这一轮猫落下的事件同一个 turn，UI 才画得出分轮横线
    （逗猫的人那条动作在外面，跟猫的反应之间正好隔开）。"""
    with world.turn():
        return _apply_locked(pid, out)


def _apply_locked(pid: str, out: dict) -> dict:
    loc = world.location_of(pid)
    reply = (out.get("reply") or "").strip()
    action = out.get("action") if out.get("action") in ACTION_TAGS else "none"

    if reply:
        try:
            world.act_mixed(pid, loc, reply)
        except Exception as e:
            logerr(f"pet_engine reply 没落上（忽略）: {e}")

    # 确定性覆盖：拉屎只在猫房生效、砂盆满了拒绝（需求继续憋着，见 pet_store.needs）
    litter = None
    poop_reset = False
    state_now = pet_store.read_state(pid)
    if action == "拉屎":
        if loc != _cat_room(pid):
            logerr(f"pet_engine：{pid} 在 {loc} 想拉屎，猫砂盆不在这，当没拉")
            action = "none"
        elif int(state_now["litter"]) >= 3:
            logerr(f"pet_engine：{pid} 砂盆满了拒绝用，继续憋着")
            action = "none"
        else:
            litter = int(state_now["litter"]) + 1
            poop_reset = True

    asleep = out["sleep"] if isinstance(out.get("sleep"), bool) else None
    new_state = pet_store.apply_interaction(
        pid, new_stats=_sanitize_stats(out.get("stats")),
        litter=litter, asleep=asleep, poop_reset=poop_reset)

    # 姿态与移动：走之前清旧的；到了新房间再摆新姿态
    move_to = (out.get("move") or "").strip() or None
    if move_to and (move_to == loc or move_to not in world.load_registry()):
        move_to = None
    pose = (out.get("pose") or "").strip()
    moved = None
    if move_to:
        _clear_pose(pid)
        try:
            moved = world.move(pid, move_to)   # 猫洞：can_enter 对 pet 恒真，不会失败
        except Exception as e:
            logerr(f"pet_engine move 失败（忽略）: {e}")
    if pose:
        _set_pose(pid, pose)
    elif asleep is False:
        _clear_pose(pid)   # 睡醒起身：躺姿快照不该留着

    log_line = (out.get("log") or reply or "").strip()
    if log_line:
        pet_store.append_petlog(pid, pid, action if action != "none" else "reply", log_line)

    return {"reply": reply, "action": action, "state": new_state,
            "moved": moved, "pose": pose or None}


# ---------- 引擎面（P2 接触发、P3 挂工具）----------
def interact(pid: str, actor: str, feed: Optional[str] = None,
             text: Optional[str] = None) -> dict:
    """直接互动（投喂三选一 / 自由文本撸猫），**要求 actor 与猫同地点**。
    工具替 actor 落房间事件（别在 MOTION 里重复——工具说明的口径）；
    猫必醒（睡着也把互动喂给模型，装不装死它自己决定）。"""
    import pet_queue   # 函数内断环：pet_queue 顶层 import 本模块
    pid = pets.match(pid) or pid
    if not pets.is_pet(pid):
        raise KeyError(f"没有这只宠物：{pid}")
    loc = world.location_of(pid)
    if world.location_of(actor) != loc:
        raise PetNotHere(f"{world.entity_name(pid)} 不在你身边")
    if feed is None and not (text or "").strip():
        raise ValueError("投喂或互动至少写一样")

    with _ENGINE_LOCK:
        pet_queue.external_for_pet(pid)   # 被直接互动 = 猫的外部输入，连发清零
        pname, aname = world.entity_name(pid), world.entity_name(actor)
        trigger = []
        if feed:
            world.append_event(loc, "system", actor, f"{aname} 给{pname}喂了{feed}",
                               kind="pet_feed")
            pet_store.append_petlog(pid, actor, "feed", f"{aname} 给{pname}喂了{feed}")
            trigger.append(f"{aname} 给你喂了{feed}")
        if (text or "").strip():
            text = text.strip()
            try:
                world.act(actor, loc, action=text)   # actor 的动作事件：在场的人都看得见
            except Exception as e:
                logerr(f"pet_engine 互动动作没落上（忽略）: {e}")
            pet_store.append_petlog(pid, actor, "interact", f"{aname} {text}")
            trigger.append(f"{aname} {text}")

        out = _parse(_call_model(pid, _build_prompt(pid, pet_store.read_state(pid),
                                                    trigger)))
        # 反应事件落地期间挂发起者护栏：他在工具结果里同轮拿到反应，事件不再唤他
        with pet_queue.interaction_guard(actor):
            res = _apply(pid, out)
        if feed:
            # 吃了东西 → 憋屎加速（确定性，不管模型给没给数值）
            res["state"] = pet_store.apply_interaction(
                pid, poop_add=pet_store.POOP_MEAL_BOOST)
        return res


def wake(pid: str, reasons: list[str]) -> dict:
    """猫的迷你醒来（P2 的 tick/事件触发调）：注入同 interact，只是触发原因不同。"""
    pid = pets.match(pid) or pid
    with _ENGINE_LOCK:
        out = _parse(_call_model(pid, _build_prompt(pid, pet_store.read_state(pid),
                                                    reasons)))
        return _apply(pid, out)


def scoop(pid: str, actor: str) -> dict:
    """铲屎（P3）：要求 **actor 人在猫房**——盆在那儿；不要求猫在场（铲的是盆不是猫）。
    落系统事件：猫在场会经事件线概率反应（pet_queue），不用特殊处理。"""
    pid = pets.match(pid) or pid
    if not pets.is_pet(pid):
        raise KeyError(f"没有这只宠物：{pid}")
    home = _cat_room(pid)
    if home is None:
        raise KeyError("没有猫房（注册表里找不到）")
    if world.location_of(actor) != home:
        raise PetNotHere("猫砂盆在猫房——人先过去再铲")
    st = pet_store.scoop(pid)
    aname = world.entity_name(actor)
    world.append_event(home, "system", actor, f"{aname} 铲了猫砂盆", kind="pet_scoop")
    pet_store.append_petlog(pid, actor, "scoop", f"{aname} 铲了猫砂盆")
    return {"state": st}


def enforce(pid: str) -> Optional[str]:
    """生存硬地板（P2 tick 每轮先调，不走模型）：饿过 FORCE_EAT_AT → 强制去猫房吃
    猫粮；憋过 POOP_FORCE_AT 且砂盆没满 → 强制解决。返回干了什么（None=没干预）。
    动作走真实物理（move 落进出事件、act 落动作事件）——强制的是行为不是数值。"""
    pid = pets.match(pid) or pid
    with _ENGINE_LOCK, world.turn():
        return _enforce_locked(pid)


def _enforce_locked(pid: str) -> Optional[str]:
    s = pet_store.read_state(pid)
    home = _cat_room(pid)
    if home is None:
        return None
    pname = world.entity_name(pid)
    if s["satiety"] <= pet_store.FORCE_EAT_AT:
        _clear_pose(pid)
        if world.location_of(pid) != home:
            world.move(pid, home)
        world.act(pid, home, action="饿得不行，埋头在自动喂食器里吃了好一阵猫粮")
        pet_store.apply_interaction(pid, new_stats={"satiety": 55},
                                    poop_add=pet_store.POOP_MEAL_BOOST)
        pet_store.append_petlog(pid, "system", "enforce", f"{pname} 饿极了，自己去吃了猫粮")
        return "force_eat"
    if float(s.get("poop_pressure", 0)) >= pet_store.POOP_FORCE_AT \
            and int(s["litter"]) < 3:
        _clear_pose(pid)
        if world.location_of(pid) != home:
            world.move(pid, home)
        world.act(pid, home, action="钻进猫砂盆解决了憋很久的大事")
        pet_store.apply_interaction(pid, litter=int(s["litter"]) + 1, poop_reset=True)
        pet_store.append_petlog(pid, "system", "enforce", f"{pname} 憋不住了，用了猫砂盆")
        return "force_poop"
    return None
