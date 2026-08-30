"""wake_sdk：醒来升 A（PLAN_sdk §5.2/PR12）——sdk 灰度角色的醒来不再起 claude -p，
改为往常驻聊天 session（chat_loop）注入一个「醒来轮」。

和老路（wake.do_wake_sync）的分工：CHAT_ENGINE 灰度到的角色走这里，其余角色老路
一行不改（过渡期双轨）。小屋总开关开着时自主醒来归 cohabit 队列，这条路只在关着时活。

三件事，各自对应设计稿的一句话：
- **注入=感知白描**（§0.3）：只给知觉材料（时间/间隔/缘由/待办），不写指令不念
  控制面板；行为契约在 chat_loop 的系统提示里一次性写死（WAKE_CONTRACT）。
  注入在**执行开始时**组装（injection_factory）——排队排了几分钟，时间那句不能是死的。
- **投递路由不靠模型输出格式**：轮来源=wake 由后端钉在 Turn.kind 上。他说的话
  （〔〕外的部分）→ outbox+窗口+Bark；〔〕包着的=心里活动，只进 wake_log（Mind 页
  素材）。夜间收敛只做在 Bark 上（§0.3「他知道你在睡觉」——投递的体贴，不是他的
  作息）；硬触发（邮件）必推。THOUGHTS/ACTION/CONTENT 四段契约退役，只剩
  [[next_wake:]] 一个约定（和聊天路同一个标记，他已经会用）。
- **抽时刻调度器**：每 tick 掷骰退役。抽样时从受「距 TA 上次说话时长」「昼夜」调制的
  分布抽一个间隔，落成 auto_wake_at 排进 schedule；TA 一说话（anchor 变了）就重抽。
  原来的静默/最小间隔/预算三类闸全部收进抽样下界与分布参数——「兑现时否决」没有了，
  也就没有 unsent 憋话簿记。

醒来轮的账（chat_loop.run 执行）：投递了消息 → 账追加一条 assistant（app 拉走 outbox
后历史里就有它，下次发送比对干净）；安静醒着 → 账不动，transcript 里那轮独处到下次
重铸自然蒸发（§5.2「纯内心不渲染」——闲念头淡忘，人同构）。
"""
import math
import random
import re
import time
import uuid
from datetime import datetime
from typing import Optional

import browser_keeper
import characters
import config
import pipeline
import state_store
from notify import bark_push, logerr

# ---------- 抽样参数（原「静默/最小间隔/预算」三类闸的新家）----------
AUTO_GAP_MIN_SEC = 45 * 60        # 下界：刚醒过/刚聊完不背靠背再醒（原 quiet/最小间隔闸）
AUTO_GAP_MAX_SEC = 8 * 3600       # 上界：再久也该醒一下看看世界
MEAN_BASE_SEC = int(3.5 * 3600)   # 刚聊完时的平均间隔（话都说完了，先过自己的日子）
MEAN_FLOOR_SEC = 100 * 60         # 静默拉长后的平均间隔（间隔越久 hazard 越高——想 TA）
SILENCE_TAU_SEC = 8 * 3600        # 静默影响的时间尺度
NIGHT_FACTOR = 1.6                # 深夜 hazard 略降的诚实理由：世界没动静、可想的事少
NIGHT_HOURS = range(1, 8)         # +省额度（§0.3：不是他的作息，他没有作息）


def _mean_gap(silence_sec: float, hour: int) -> float:
    mean = MEAN_FLOOR_SEC + (MEAN_BASE_SEC - MEAN_FLOOR_SEC) * math.exp(
        -max(0.0, silence_sec) / SILENCE_TAU_SEC)
    if hour in NIGHT_HOURS:
        mean *= NIGHT_FACTOR
    return mean


def sample_gap(silence_sec: float, hour: int, rng=random) -> int:
    """抽一个「多久后自己醒」的间隔（秒）。下界之上指数分布——无记忆性正好是
    「每一刻都可能想起你」的形状，均值由静默时长/昼夜调制。"""
    mean = _mean_gap(silence_sec, hour)
    gap = AUTO_GAP_MIN_SEC + rng.expovariate(1.0 / max(60.0, mean - AUTO_GAP_MIN_SEC))
    return int(min(gap, AUTO_GAP_MAX_SEC))


def _last_user_ts(cid: str) -> int:
    """TA 最后一次说话的时刻（抽样锚点：它一变=有新交互，重抽）。"""
    window = state_store.read_recent_window(cid)
    return next((int(m["ts"]) for m in reversed(window)
                 if m.get("role") == "user" and m.get("ts")), 0)


def resample_auto(cid: str, now: Optional[float] = None, why: str = "") -> int:
    now_i = int(now or time.time())
    anchor = _last_user_ts(cid)
    hour = datetime.now(config.APP_TZ).hour
    gap = sample_gap(float(now_i - anchor) if anchor else 0.0, hour)
    with state_store.SCHEDULE_LOCK:
        sched = state_store.read_schedule(cid)
        sched["auto_wake_at"] = now_i + gap
        sched["auto_anchor_ts"] = anchor
        state_store.write_schedule(sched, cid)
    logerr(f"wake_sdk 抽下次自醒（{cid}，{why}）：{pipeline.fmt_gap(gap)}后")
    return now_i + gap


def maybe_auto(cid: str, now: Optional[float] = None) -> bool:
    """tick 里问一句「到点了吗」。没抽过/TA 说过新话 → 重抽不醒；到点 → 消费掉
    这个点并入队（醒完 finish_wake_turn 会重抽；先抹掉防下个 tick 连发）。"""
    now = now or time.time()
    sched = state_store.read_schedule(cid)
    anchor = _last_user_ts(cid)
    at = sched.get("auto_wake_at")
    if at is None or int(sched.get("auto_anchor_ts") or -1) != anchor:
        resample_auto(cid, now, why="没抽过" if at is None else "有新交互")
        return False
    if now < float(at):
        return False
    with state_store.SCHEDULE_LOCK:
        sched = state_store.read_schedule(cid)
        sched["auto_wake_at"] = None
        state_store.write_schedule(sched, cid)
    return enqueue_wake(cid, "auto")


# ---------- 注入（感知白描，执行开始时组装）----------

def wake_injection(cid: str, trigger: str, note: str = "") -> str:
    now_ts = int(time.time())
    window = state_store.read_recent_window(cid)
    line = f"〔现在是 {pipeline.now_str()}。"
    last_ts = next((int(m["ts"]) for m in reversed(window) if m.get("ts")), 0)
    if last_ts and now_ts > last_ts:
        line += f"距上一条消息过了 {pipeline.fmt_gap(now_ts - last_ts)}。"
    line += "〕"
    if note:
        line += f"\n〔{note}〕"
    if trigger == "scheduled":
        # NEXT 是他主动给自己留话的口——按感知语气渲染成他自己的念头（§5.2）。
        todo = (state_store.read_schedule(cid).get("next_wake_todo") or "").strip()
        line += (f"\n〔你之前想着这会儿要：{todo}〕" if todo
                 else "\n〔这个点是你之前自己定下要醒的〕")
    return line


# ---------- 投递（〔〕内外分流）----------

_MUSING_RE = re.compile(r"〔[^〔〕]*〕")


def split_musings(text: str) -> tuple[str, str]:
    """〔〕外的话（会发给 TA）/〔〕内的心里活动（只进 wake_log）。"""
    musings = [m.group(0)[1:-1].strip() for m in _MUSING_RE.finditer(text or "")]
    said = _MUSING_RE.sub("", text or "").strip()
    return said, "\n".join(m for m in musings if m)


def finish_wake_turn(cid: str, trigger: str, force: bool, started_ts: int,
                     reply: str, stored: list[dict]) -> str:
    """醒来轮收尾（chat_loop 在 finalize 回调里调）。返回投递出去的正文
    （空串=这次安静醒着）——chat_loop 拿它决定账里追不追加 assistant 条。"""
    now_ts = int(time.time())
    settings = state_store.load_settings(cid)

    # NEXT：口径同 finalize_chat_reply（同一个标记，他在聊天里已经会用）。
    reply, next_min, next_raw, next_todo = pipeline.parse_chat_next(reply or "")
    next_wake_at = (now_ts + next_min * 60) if next_min else None
    nw_note = pipeline.next_wake_note(next_raw, next_wake_at) if next_wake_at else ""

    # 浏览器去留标记 + 本轮逛过的网页（口径同 do_wake_sync）。
    reply, browser_choice = pipeline.parse_browser_markers(reply)
    browse_urls: list[str] = []
    for s in stored or []:
        if s.get("tool") == "browse" and s.get("ok", True):
            u = (s.get("text") or "").strip()
            if u and u not in browse_urls:
                browse_urls.append(u)
    if browse_urls:
        try:
            state_store.append_browse_log({"ts": now_ts, "time": pipeline.now_str(),
                                           "source": "wake", "urls": browse_urls},
                                          char_id=cid)
        except Exception as e:
            logerr(f"记 browse_log 失败: {e}")
    browser_keeper.apply_choice(browser_choice, browsed=bool(browse_urls), char_id=cid)

    said, musing = split_musings(reply)

    # wake_log 扩展字段（PR12 先行，PR13 归一进活动事件账本）：acts=这轮碰了外部世界
    # 的工具调用（时间+轮来源在条目上，工具+对象摘要在这），行为留痕不靠他自觉。
    acts = [{"tool": s.get("tool"), "ok": bool(s.get("ok", True)),
             "text": (s.get("text") or "")[:80]} for s in (stored or [])]
    mem_stored = [s for s in (stored or [])
                  if s.get("tool") not in pipeline.NON_MEMORY_TOOLS]
    base: dict = {"ts": started_ts, "time": pipeline.now_str(), "source": "wake",
                  "trigger": trigger, "engine": "sdk", "thoughts": musing}
    if acts:
        base["acts"] = acts
    if mem_stored:
        base["stored"] = mem_stored
    if browse_urls:
        base["browse"] = browse_urls
    if next_wake_at:
        base["next_wake_at"] = next_wake_at
        base["next_wake_note"] = nw_note

    delivered = ""
    if said:
        app_text, sticker_ids, bark_text = pipeline.split_wake_stickers(
            said, pipeline.sticker_handle_map(state_store.read_sticker_catalog()))
        if not app_text and not sticker_ids:
            app_text = bark_text = said
        app_text = pipeline.strip_markers(app_text).strip()
        if app_text or sticker_ids:
            state_store.outbox_append({"id": uuid.uuid4().hex[:12], "ts": started_ts,
                                       "text": app_text, "sticker_ids": sticker_ids,
                                       "char_id": cid, "delivered": False,
                                       "origin": "wake"})
            with state_store.WINDOW_LOCK:
                window = state_store.read_recent_window(cid)
                window.append({"role": "assistant", "text": (bark_text or app_text),
                               "ts": started_ts})
                state_store.write_recent_window(window, cid)
            # 夜间收敛只收 Bark（§0.3）：消息照进 outbox/历史，TA 早上自然看到；
            # 硬触发（到点必须说的事）不分昼夜必推。
            import wake as wake_mod
            night = not wake_mod.is_daytime(settings, datetime.now(config.APP_TZ))
            bark_ok = False
            if force or not night:
                bark_ok = bark_push(bark_text or app_text,
                                    title=characters.display_name(cid))
            state_store.append_wake_log({**base, "action": "message", "pushed": True,
                                         "content": app_text[:500], "bark": bool(bark_ok)},
                                        char_id=cid)
            delivered = app_text
    if not delivered:
        state_store.append_wake_log({**base, "action": "none"}, char_id=cid)

    # 调度簿记（口径同 do_wake_sync 尾段）：定了新 NEXT 就整体替换；到点醒来那次
    # 只消费已过期的点；随机醒且没给新 NEXT 则原样保留待命的点。
    with state_store.SCHEDULE_LOCK:
        sched = state_store.read_schedule(cid)
        sched["last_wake_at"] = now_ts
        cur_next = sched.get("next_wake_at")
        if next_wake_at is not None:
            sched["next_wake_at"] = next_wake_at
            sched["next_wake_todo"] = next_todo
        elif trigger == "scheduled" and (cur_next is None or float(cur_next) <= now_ts):
            sched["next_wake_at"] = None
            sched["next_wake_todo"] = ""
        state_store.write_schedule(sched, cid)
    resample_auto(cid, why="醒完重抽")
    return delivered


def _wake_dead(cid: str, trigger: str) -> None:
    """醒来轮没走完（超时/流断/引擎错）：记 error 心流 + 冷却 30 分钟
    （口径同 do_wake_sync 的错误退避，防持续失败时每 tick 白醒一次）。"""
    now_ts = int(time.time())
    logerr(f"sdk 醒来轮没走完（{cid}，{trigger}）：记 error + 冷却 30 分钟")
    try:
        state_store.append_wake_log({"ts": now_ts, "time": pipeline.now_str(),
                                     "source": "wake", "action": "error",
                                     "trigger": trigger, "engine": "sdk"}, char_id=cid)
    except Exception as e:
        logerr(f"记 wake error 失败: {e}")
    with state_store.SCHEDULE_LOCK:
        sched = state_store.read_schedule(cid)
        sched["last_wake_at"] = now_ts
        sched["cooldown_until"] = now_ts + 1800
        state_store.write_schedule(sched, cid)


# ---------- 入队 ----------

def enqueue_wake(cid: str, trigger: str, note: str = "", force: bool = False) -> bool:
    """把一次醒来排进该角色的聊天 session。必须在事件循环上调（maybe_wake 就在）。
    session 没开 → chat_loop 惰性从镜像开一个（「由 wake 唤起 resume」归 session 管理层）。
    失败返回 False（调用方压冷却，别每 tick 硬试）。"""
    import chat_loop
    started = {"ts": 0}

    def _injection() -> str:
        started["ts"] = int(time.time())
        return wake_injection(cid, trigger, note)

    def _finalize(reply: str, stored: list) -> dict:
        try:
            delivered = finish_wake_turn(cid, trigger, force,
                                         started["ts"] or int(time.time()),
                                         reply, stored)
        except Exception as e:
            logerr(f"醒来投递收尾失败（{cid}）: {e}")
            delivered = ""
        return {"reply": delivered}

    try:
        turn = chat_loop.Turn(
            rid=f"wake-{trigger}-{uuid.uuid4().hex[:8]}", history=[], new_msg={},
            injection="", finalize=_finalize, kind="wake",
            injection_factory=_injection, on_dead=lambda: _wake_dead(cid, trigger))
        handle = chat_loop._ensure_loop(cid)
        handle.queue.put_nowait(turn)
        logerr(f"sdk 醒来入队（{cid}，{trigger}{'，硬触发' if force else ''}）")
        return True
    except Exception as e:
        logerr(f"sdk 醒来入队失败（{cid}，{trigger}）: {e}")
        _wake_dead(cid, trigger)
        return False
