"""
wake 调度器：模型自己"醒来"——主动决定要不要给用户发消息（主动性的核心）。

省 token 的关键设计：**预闸门在叫模型之前判掉**（概率/时段/静默/最小间隔都是纯本地判断），
只有真要醒才起一次 claude -p。醒来的输出走 THOUGHTS / ACTION / CONTENT / NEXT 四段协议：
内心永远记日志（用户在 app 里看不到但后端有账），只有 ACTION=message 才真正推送；
推送前还有硬顶闸（每天条数 + 最小间隔），拦推送、不拦思考。

工作集永远有界：醒来是后台反复跑，绝不塞全量历史——只用 recent_window 切片。
"""
import asyncio
import random
import re
import subprocess
import time
import uuid
from datetime import datetime
from typing import Optional

import config
import pipeline
import state_store
from notify import bark_push, logerr

WAKE_TICK_SEC = config.WAKE_TICK_SEC
MIN_WAKE_GAP_SEC = 180                                    # 两次醒来最小间隔（防背靠背撞）
FREQ_PROB_15 = {"low": 0.08, "mid": 0.20, "high": 0.40}   # 按 15min 一次校准的醒来概率（按 tick 缩放）


# ---------- 主 chat 活跃轮计数（wake 避让用）----------
# chat 轮进行中 wake 撞进来，会拿着过期 recent_window 说胡话——避让到下个 tick。
_chat_turns = {"n": 0}


def chat_turn_begin() -> None:
    _chat_turns["n"] += 1


def chat_turn_end() -> None:
    _chat_turns["n"] = max(0, _chat_turns["n"] - 1)


def chat_turn_active() -> bool:
    return _chat_turns["n"] > 0


# ---------- 时段 ----------
def _parse_hhmm(s: str) -> int:
    """'HH:MM' → 当天分钟数（0..1440）。'24:00' → 1440。"""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def is_daytime(settings: dict, now: datetime) -> bool:
    """按 active_start/active_end 判断现在算活跃时段还是夜间。支持跨零点区间。"""
    start = _parse_hhmm(settings["active_start"])
    end = _parse_hhmm(settings["active_end"])
    cur = now.hour * 60 + now.minute
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end   # 跨零点，如 22:00~06:00


# ---------- 醒来提示词 ----------
def wake_prompt(settings: dict) -> str:
    u = config.user_name()
    now_str = pipeline.now_str()
    window = state_store.read_recent_window()

    gap_line = ""
    if window and window[-1].get("ts"):
        gap_line = f"距离你和{u}上次说话，过了 {pipeline.fmt_gap(int(time.time()) - int(window[-1]['ts']))}。"

    # wake 注入条数：用户可调，默认 50；夹取防手滑（容量上限见 state_store.RECENT_WINDOW_N）。
    wake_n = min(max(int(settings.get("wake_window_n") or 50), 20), 300)
    timeline = pipeline.build_context_timeline(window[-wake_n:])
    timeline_block = timeline if timeline else "（最近没有对话）"

    now = datetime.now(config.APP_TZ)
    freq_cn = {"low": "低（少打扰）", "mid": "中", "high": "高（可以勤快点）"}.get(
        settings.get("day_freq" if is_daytime(settings, now) else "night_freq", "low"), "中")

    # 长期记忆（Ombre 挂上才有）：引导 + 近 12h 已存清单——不给清单模型会把
    # 时间线里同一件事每次醒来都存一遍（mianmian 实踩）。
    memory_section = ""
    if pipeline.ombre_alive():
        lines = ["【你有自己的长期记忆（Ombre 工具）：想不起细节可以先 breath；"
                 "这次醒来若有值得留住的，用 hold 存下来。】"]
        recent_stored = [s.get("text", "") for w in state_store.read_wake_log(limit=100)
                         if int(w.get("ts", 0)) > int(time.time()) - 12 * 3600
                         for s in (w.get("stored") or [])]
        if recent_stored:
            lines.append("【最近 12 小时你已经存过这些，别重复存：" +
                         "；".join(t[:60] for t in recent_stored[-8:] if t) + "】")
        memory_section = "\n" + "\n".join(lines) + "\n"

    # 表情库（持久化的清单；醒来发消息也能配表情）。不给改描述——那是聊天里的事。
    sb = pipeline.sticker_block(state_store.read_sticker_catalog(), allow_desc=False)
    sticker_section = f"\n{sb}\n" if sb else ""

    # 这轮发消息会被打扰控制拦下 → 先告诉他再让他想（闸门只拦推送不拦思考，但他有权知道）。
    blocked = push_block(settings)
    blocked_section = f"\n{blocked[1]}\n" if blocked else ""

    return f"""【这是一次你自己的醒来，不是{u}发来的消息】
现在是 {now_str}。{gap_line}
{pipeline.pronoun_hint()}

【最近发生的，按时间顺序——对话 / 你自己醒来时的内心，看时间戳别搞混先后】
{timeline_block}
{memory_section}{sticker_section}{blocked_section}
想清楚这次要不要做点什么。想{u}了、有话想说就发消息；没什么可说的就安静醒着，不用硬找话。
你还可以自己定下次醒来的时间（NEXT）：写了我保证到那个点把你醒一次；这中间你照样可能随机醒来，不受影响。范围 5 分钟~12 小时；没特别想法就写"无"（不定这个点，纯随机节奏）。{u}现在设的活跃频率偏好是「{freq_cn}」，你定 NEXT 时可以参考。
严格按下面格式回答（四段都要，标签用英文、后跟冒号）：
THOUGHTS: <你此刻真实的内心，几句话>
ACTION: <none / message，二选一>
CONTENT: <ACTION=message 就写要发给{u}的话；=none 留空>
NEXT: <你希望多久后再醒来，如 "90分钟" 或 "3小时"；没想法写 "无">
"""


def parse_wake_output(text: str) -> tuple[str, str, str, Optional[int], str]:
    """从模型输出里切出 THOUGHTS / ACTION / CONTENT / NEXT(分钟) / NEXT原话。解析失败一律安全兜底。"""
    labels = ["THOUGHTS", "ACTION", "CONTENT", "NEXT"]
    idx = {}
    for lb in labels:
        m = re.search(rf"(?im)^\s*{lb}\s*[:：]", text)
        if m:
            idx[lb] = (m.start(), m.end())

    def section(lb: str) -> str:
        if lb not in idx:
            return ""
        start = idx[lb][1]
        laters = [idx[o][0] for o in labels if o in idx and idx[o][0] > idx[lb][0]]
        end = min(laters) if laters else len(text)
        return text[start:end].strip()

    thoughts = section("THOUGHTS")
    action_raw = section("ACTION").lower()
    content = section("CONTENT")
    next_raw = section("NEXT")
    next_min = pipeline.parse_next_minutes(next_raw)
    action = "message" if "message" in action_raw else "none"
    return thoughts, action, content, next_min, next_raw.strip()


# ---------- 起模型 ----------
def run_claude_wake(prompt: str) -> tuple[Optional[str], list[dict]]:
    """起一次 claude -p（醒来用），collect_all_text 拼接全部文本段。失败/超时返回 (None, [])，不抛。"""
    args = pipeline.base_claude_args() + ["--output-format", "stream-json", "--verbose"]
    try:
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True,
                              env=pipeline._subprocess_env(), timeout=config.CLAUDE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        logerr("醒来调用超时")
        return None, []
    if proc.returncode != 0:
        # stderr 常是空的（stream-json 模式报错走 stdout）——两头都记，排障不绕路。
        logerr(f"醒来进程出错 rc={proc.returncode}: stderr={proc.stderr[:200]!r} stdout尾={proc.stdout[-300:]!r}")
        return None, []
    return pipeline.parse_claude_stream(proc.stdout, collect_all_text=True)


# ---------- 分发 ----------
def _date_str(ts: int) -> str:
    """epoch → 配置时区的 YYYY-MM-DD（"今天推了几条"统计用）。"""
    return datetime.fromtimestamp(ts, config.APP_TZ).strftime("%Y-%m-%d")


def push_block(settings: dict) -> Optional[tuple[str, str]]:
    """算这会儿若要推送会被哪道闸拦。返回 (机器码, 给模型看的提示句)；None=不会被拦。
    醒来拼 prompt 前算好注入——闸门只拦推送不拦思考，但他有权先知道再决定做什么，
    别让他以为发出去了（实锤缺陷：被拦的消息在他下轮时间线里看着像发过）。"""
    now_ts = int(time.time())
    u = config.user_name()
    tail = "——本轮你发消息将不会被推送，发了什么 ta 也收不到。】"
    # 只读尾部（append-only 日志会一直变长，全量 parse 违背"工作集有界"）；
    # 1000 条覆盖好几天的醒来记录，每日计数/最近间隔都够。
    pushed_logs = [w for w in state_store.read_wake_log(limit=1000)
                   if w.get("action") == "message" and w.get("pushed")]

    dm = settings.get("daily_max")
    if dm is not None:
        today = _date_str(now_ts)
        if sum(1 for w in pushed_logs if _date_str(int(w.get("ts", 0))) == today) >= dm:
            return ("capped_daily",
                    f"【{u}设置了「主动消息每天最多 {dm} 条」，今天已达上限{tail}")

    mi = settings.get("min_interval_min")
    if mi is not None and pushed_logs:
        last_ts = max(int(w.get("ts", 0)) for w in pushed_logs)
        if now_ts - last_ts < mi * 60:
            return ("capped_interval",
                    f"【{u}设置了「主动消息最小间隔 {mi} 分钟」，距上一条还没到{tail}")

    q = settings.get("quiet_after_user_min")
    if q:
        window = state_store.read_recent_window()
        user_ts = next((int(m["ts"]) for m in reversed(window)
                        if m.get("role") == "user" and m.get("ts")), None)
        if user_ts is not None and now_ts - user_ts < q * 60:
            return ("capped_quiet",
                    f"【{u}设置了「刚聊过后静默 {q} 分钟」，ta 刚说过话{tail}")
    return None


def try_push(text: str, settings: dict, thoughts: str = "", trigger: str = "",
             sticker_ids: Optional[list] = None, bark_text: str = "",
             next_wake_at: Optional[int] = None, next_wake_note: str = "",
             started_ts: Optional[int] = None, stored: Optional[list] = None) -> bool:
    """硬顶闸（每天条数 + 最小间隔 + 静默）。只拦推送、不拦思考。通过 → 进 outbox + Bark + 追加窗口。
    thoughts＝这次醒来的内心，一并记进日志（连被抑制的也记）。
    sticker_ids＝这条消息附带的表情（app 按 id 取本地图上屏）；
    bark_text＝通知用文案（表情标记换成 [sticker_sN] 占位，通知里显示不了图）。
    started_ts＝这次醒来的起始时刻：①outbox/窗口时间戳用它，app 按 ts 插位会把消息放回
    他"开始想"的位置（修 MAB 乱序）；②生成期间用户发了新消息 → 整条不发（内容已过时）。"""
    now_ts = int(time.time())
    started_ts = started_ts or now_ts
    # 空消息闸：CONTENT 全是无效标记时层层剥完可能只剩空串——空气泡不推。
    if not text.strip() and not (sticker_ids or []):
        logerr("message 被抑制：剥完标记后为空")
        return False
    base = {"ts": now_ts, "time": pipeline.now_str(), "source": "wake", "action": "message",
            "trigger": trigger, "thoughts": thoughts}
    if stored:
        base["stored"] = stored   # 这次醒来存/改了什么记忆（Mind 展示 + "别重复存"清单）
    if next_wake_at:
        base["next_wake_at"] = next_wake_at
        base["next_wake_note"] = next_wake_note

    # 醒来生成期间（几十秒）用户发了新消息 → 这条讲的是旧世界的事，发出去必乱序还答非所问。
    # 整条抑制，原文记日志可查。
    window = state_store.read_recent_window()
    fresh_user = next((int(m["ts"]) for m in reversed(window)
                       if m.get("role") == "user" and m.get("ts")
                       and int(m["ts"]) > started_ts), None)
    if fresh_user is not None:
        state_store.append_wake_log({**base, "content": text[:500], "pushed": False,
                                     "note": "stale_user_msg"})
        logerr("message 被抑制：生成期间用户发了新消息（stale）")
        return False

    blocked = push_block(settings)
    if blocked:
        note, _ = blocked
        # 被拦的正文也记进日志——不然事后（Mind 页）只知道他想发、不知道他想说什么。
        state_store.append_wake_log({**base, "content": text[:500], "pushed": False, "note": note})
        logerr(f"message 被抑制：{note}")
        return False

    state_store.outbox_append({"id": uuid.uuid4().hex[:12], "ts": started_ts, "text": text,
                               "sticker_ids": sticker_ids or [],
                               "delivered": False, "origin": "wake"})
    # 窗口记他自己视角的一句（带 [sticker_sN] 占位），方便之后回忆自己发过表情。
    # 加锁：chat finalize 可能在并发覆盖窗口，读改写不锁会互踩丢条。
    with state_store.WINDOW_LOCK:
        window = state_store.read_recent_window()
        window.append({"role": "assistant", "text": (bark_text or text), "ts": started_ts})
        state_store.write_recent_window(window)
    ok = bark_push(bark_text or text)
    state_store.append_wake_log({**base, "pushed": True, "bark": ok})
    return True


def do_wake_sync(settings: dict, trigger: str) -> dict:
    """真正的醒来：起模型 → 解析 → 分发。同步阻塞（在线程池里跑，别放主事件循环）。"""
    now_ts = int(time.time())

    raw, stored = run_claude_wake(wake_prompt(settings))
    # 网页操作不进日志（同 chat 侧口径）：心流日志只记记忆类操作
    stored = [s for s in (stored or []) if s.get("tool") != "webpage"]

    if raw is None:
        state_store.append_wake_log({"ts": now_ts, "time": pipeline.now_str(), "source": "wake",
                                     "action": "error", "trigger": trigger})
        # 错误退避 30 分钟：最小间隔闸(180s)小于 tick(300s) 拦不住重试——claude 登录态过期
        # 这类持续失败场景会每个 tick 起一次注定失败的子进程。不动他自定的 next_wake_at。
        with state_store.SCHEDULE_LOCK:
            sched = state_store.read_schedule()
            sched["last_wake_at"] = now_ts
            sched["cooldown_until"] = now_ts + 1800
            state_store.write_schedule(sched)
        return {"action": "error"}

    thoughts, action, content, next_min, next_raw = parse_wake_output(raw)
    # 这次顺带定的下次醒来：算出绝对时间点 + 现成提示文案。
    next_wake_at = (now_ts + next_min * 60) if next_min else None
    nw_note = pipeline.next_wake_note(next_raw, next_wake_at) if next_wake_at else ""
    result = {"action": action, "thoughts": thoughts, "content": content,
              "trigger": trigger, "next_min": next_min}

    if action == "message" and content.strip():
        # 他可能在消息里配了表情 [[sticker:sN]] → 拆成 app 文字 + 表情 id + 通知文案。
        app_text, sticker_ids, bark_text = pipeline.split_wake_stickers(
            content.strip(), pipeline.sticker_handle_map(state_store.read_sticker_catalog()))
        if not app_text and not sticker_ids:   # 标记无效/解析空 → 原文照发
            app_text = bark_text = content.strip()
        app_text = pipeline.strip_markers(app_text).strip()
        result["pushed"] = try_push(app_text, settings, thoughts, trigger,
                                    sticker_ids=sticker_ids, bark_text=bark_text,
                                    next_wake_at=next_wake_at, next_wake_note=nw_note,
                                    started_ts=now_ts, stored=stored)
    else:
        result["action"] = "none"   # none，或 message 但 content 空 → 兜底成 none
        entry = {"ts": now_ts, "time": pipeline.now_str(), "source": "wake", "action": "none",
                 "trigger": trigger, "thoughts": thoughts}
        if stored:
            entry["stored"] = stored
        if next_wake_at:
            entry["next_wake_at"] = next_wake_at
            entry["next_wake_note"] = nw_note
        state_store.append_wake_log(entry)

    # 记调度：本次醒来时间 + 待命的下次 scheduled 点（加锁读改写）。
    # NEXT 只保证到点醒一次、不压随机。这轮明确定了新 NEXT → 用新的；到点醒来那次 → 只消费
    # **已过期的**点（生成这几十秒里聊天中若刚定了新的未来点，不能被静默冲掉）；
    # 随机醒来且没给新 NEXT（写"无"）→ 保留原先待命的点。
    with state_store.SCHEDULE_LOCK:
        sched = state_store.read_schedule()
        sched["last_wake_at"] = now_ts
        cur_next = sched.get("next_wake_at")
        if next_wake_at is not None:
            sched["next_wake_at"] = next_wake_at
        elif trigger == "scheduled" and (cur_next is None or float(cur_next) <= now_ts):
            sched["next_wake_at"] = None
        state_store.write_schedule(sched)
    return result


# ---------- 预闸门（不叫模型）----------
async def maybe_wake() -> None:
    # 主 chat 轮进行中 → 避让：wake 撞进来会拿着过期 recent_window 说胡话。下个 tick 再看。
    if chat_turn_active():
        logerr("wake 避让：主 chat 轮进行中，本 tick 跳过")
        return
    settings = state_store.load_settings()
    if not settings.get("enabled", True):
        return

    now = time.time()
    sched = state_store.read_schedule()

    # 最小间隔闸：距上次醒来太近就跳过（防两种醒来背靠背撞）。
    if now - float(sched.get("last_wake_at") or 0) < MIN_WAKE_GAP_SEC:
        return
    # 错误退避：上次醒来失败后冷却期内不再试（防持续失败时无限起子进程）。
    if now < float(sched.get("cooldown_until") or 0):
        return

    # 决定这一 tick 要不要醒、谁触发：
    # NEXT 只保证到点 scheduled 醒一次，不压随机；没到点就照常走概率（每 tick 独立掷）。
    # ⚠️ 定时醒不受"刚聊过静默"限制——他自己定的点必须醒（静默只是别让他推送打扰，
    # 那道在推送层拦 + 提示句告知，见 push_block）。静默期只省掉随机醒（不叫模型，省 token）。
    next_wake = sched.get("next_wake_at")
    trigger = None
    if next_wake is not None and now >= float(next_wake):
        trigger = "scheduled"
    else:
        # 用户刚说过话就别随机戳（往回找最后一条 user 消息——window 末条通常是模型自己的回复）。
        q = settings.get("quiet_after_user_min")
        if q:
            window = state_store.read_recent_window()
            user_ts = next((int(m["ts"]) for m in reversed(window)
                            if m.get("role") == "user" and m.get("ts")), None)
            if user_ts is not None and now - user_ts < q * 60:
                return
        # 走概率（按 tick 缩放，保持"每小时期望醒几次"和 15min 校准一致）。
        local_now = datetime.now(config.APP_TZ)
        freq = settings.get("day_freq" if is_daytime(settings, local_now) else "night_freq", "low")
        prob = FREQ_PROB_15.get(freq, 0.08) * (WAKE_TICK_SEC / 900)
        if random.random() < prob:
            trigger = "probability"

    if trigger:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, do_wake_sync, settings, trigger)


async def scheduler_loop() -> None:
    logerr(f"wake 调度器启动，tick={WAKE_TICK_SEC}s")
    while True:
        try:
            await asyncio.sleep(WAKE_TICK_SEC)
            await maybe_wake()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logerr(f"tick 出错（忽略，继续下一轮）: {e}")
