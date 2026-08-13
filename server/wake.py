"""
wake 调度器：模型自己"醒来"——主动决定要不要给用户发消息（主动性的核心）。

省 token 的关键设计：**预闸门在叫模型之前判掉**（概率/时段/静默/最小间隔都是纯本地判断），
只有真要醒才起一次 claude -p。醒来的输出走 THOUGHTS / ACTION / CONTENT / NEXT 四段协议：
内心永远记日志（进 wake_log，app 的「心流日志」页会展示给用户），只有 ACTION=message 才真正推送；
推送前还有硬顶闸（每天条数 + 最小间隔），拦推送、不拦思考。

工作集永远有界：醒来是后台反复跑，绝不塞全量历史——只用 recent_window 切片。

醒来分两种，别混：
- **自发的**（随机概率 / 他自己定的 NEXT）：所有软闸都拦得住它，code 模式开着时整个避让。
- **硬触发的**（force=True，留给日程提醒这类到点必须说的事）：绕开全部软闸，
  但要在 prompt 里如实告诉他当下的处境（比如 code 会话还开着）。
"""
import asyncio
import random
import re
import subprocess
import time
import uuid
from datetime import datetime
from typing import Optional

import functools

import browser_keeper
import characters
import code_bridge
import config
import mail_bridge
import pipeline
import plugins
import state_store
from notify import bark_push, logerr

WAKE_TICK_SEC = config.WAKE_TICK_SEC
MIN_WAKE_GAP_SEC = 180                                    # 两次醒来最小间隔（防背靠背撞）
FREQ_PROB_15 = {"low": 0.08, "mid": 0.20, "high": 0.40}   # 按 15min 一次校准的醒来概率（按 tick 缩放）


def _cid(char_id: Optional[str]) -> str:
    return char_id or state_store.DEFAULT_CHAR_ID


# ---------- 主 chat 活跃轮计数（wake 避让用，per 角色）----------
# chat 轮进行中 wake 撞进来，会拿着过期 recent_window 说胡话——避让到下个 tick。
# 按角色分开数：Cass 在聊天不该拦住别人醒来（各聊各的、各有各的窗口）。
_chat_turns: dict[str, int] = {}


def chat_turn_begin(char_id: Optional[str] = None) -> None:
    _chat_turns[_cid(char_id)] = _chat_turns.get(_cid(char_id), 0) + 1


def chat_turn_end(char_id: Optional[str] = None) -> None:
    _chat_turns[_cid(char_id)] = max(0, _chat_turns.get(_cid(char_id), 0) - 1)


def chat_turn_active(char_id: Optional[str] = None) -> bool:
    return _chat_turns.get(_cid(char_id), 0) > 0


# ---------- code 模式（wake 避让 + prompt 告知用）----------
_code_avoid: dict[str, bool] = {}   # 避让日志只在进入 code 模式那次打一条，别每 tick 刷屏（per 角色）
_budget_hit: dict[str, bool] = {}   # 醒来预算耗尽的日志同理：只在撞上那次打一条（per 角色）


def code_session_open() -> bool:
    """code 模式此刻开着吗。「会话活着 = 模式开着」是条干净的不变量（app 也靠它对齐）。

    判据用 session_alive() 不用 is_busy()：后者要隔 0.8 秒抓两帧画面比对，每个 tick 都
    跑太贵；而且"会话开着但停着等人"也不该被自发的醒来插一条——那会和他在 code 会话里
    说的话挤在同一个聊天框里。
    探不出来时**当作没开**（放行）：反过来兜底的话，tmux 一出岔子他就再也不醒了，
    而且从外面完全看不出为什么。"""
    try:
        return code_bridge.session_alive()
    except Exception as e:
        logerr(f"探 code 会话失败（当作没开，放行）: {e}")
        return False


def code_session_block(forced: bool) -> str:
    """code 模式开着时注入醒来 prompt 的那一段。没开则空串。

    正常情况下自发的醒来在 maybe_wake 就被避让掉了，所以这段实际只出现在硬触发的醒来里。
    但判据仍写成「会话开着就注入」而不是「forced 就注入」——prompt 该照实说当下的世界，
    以后万一有哪条路绕过了避让闸，它也不会跟着说谎。"""
    if not code_session_open():
        return ""
    u = config.user_name()
    # game 档案借的是同一套会话基建：措辞跟着档案走，别对着游戏会话说「电脑上的 code 会话」。
    is_game = code_bridge.active_profile() == "game"
    kind = "游戏" if is_game else "code"
    # 这儿值得花 is_busy() 那 0.8 秒：硬触发的醒来很稀罕，而"正跑着活"和"停着等人"
    # 该说的话完全不一样。探不出来就含糊带过，别瞎猜一个状态给他。
    try:
        busy = code_bridge.is_busy()
        state = ("你正玩着呢" if is_game else "你正在那边跑一个活") if busy \
            else "你停在那边、等着 TA 说话"
    except Exception:
        state = "你人在那边"
    why = ("这次不是随机醒来——是有件到点必须说的事把你叫起来的。" if forced
           else "（按理说这会儿你不该被随机醒来打断，出现这句说明有别的东西叫醒了你。）")
    return (f"\n【注意：你此刻开着一个{kind}会话，{state}。{why}\n"
            f"你在这里说的话、和你在{kind}会话里说的话，进的是**同一个聊天框**——"
            f"那边刚说过的别再说一遍，也别跟那边的话打架。\n"
            f"另外这次醒来你手上没有那边的工具，只能说话。】\n")


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


# ---------- 上次没送出去的话 ----------
# 被拦（撞上用户先开口 / 打扰控制）的正文不该白写：下次醒来摆到他面前，
# 他自己决定重说、改说还是放弃。**不硬发**——插回时间线会让他对自己的言行失忆
# （他当时并不知道自己"说过"），也会跟用户刚说的话撞车。
UNSENT_MAX_AGE_SEC = 3 * 3600   # 超过 3 小时的就算了，世界已经变了

_UNSENT_WHY = {
    "stale_user_msg": "正好撞上{u}先开口了",
    "capped_daily": "今天的主动消息条数用完了",
    "capped_interval": "距上一条主动消息还没到最小间隔",
    "capped_quiet": "{u}刚说过话、还在静默期里",
}


def unsent_block(u: str, char_id: Optional[str] = None) -> str:
    """上次想说但没送出去的那段话（给醒来 prompt 用）。没有则空串。
    只看**最后一条** message 记录：后来成功发过消息，这事自然就翻篇了。"""
    last_msg = next((w for w in reversed(state_store.read_wake_log(limit=50, char_id=char_id))
                     if w.get("action") == "message"), None)
    if not last_msg or last_msg.get("pushed"):
        return ""
    text = (last_msg.get("content") or "").strip()
    if not text:
        return ""
    if int(time.time()) - int(last_msg.get("ts", 0)) > UNSENT_MAX_AGE_SEC:
        return ""
    why = _UNSENT_WHY.get(last_msg.get("note", ""), "被打扰控制拦下了").format(u=u)
    when = pipeline.fmt_ts(int(last_msg.get("ts", 0)))
    return (f"\n【你上次醒来（{when}）想说这段话，但{why}，没送出去：\n"
            f"「{text}」\n"
            f"现在还想说吗？还合适就重说一遍（措辞可以改），过时了就算了、别硬凑。】\n")


# ---------- 醒来提示词 ----------
def wake_prompt(settings: dict, forced: bool = False, note: str = "",
                char_id: Optional[str] = None) -> str:
    """forced＝这是一次硬触发的醒来（绕开了所有软闸）。影响两处：打扰控制那句不再注入
    （闸对它不生效，说了就是骗他），以及 code 会话那段的措辞。
    note＝硬触发的缘由（如「有新邮件」），非空就整段注入——不告诉他为什么醒，
    他只会当成一次普通的随机醒来，到点的事就黄了。"""
    u = config.user_name()
    now_str = pipeline.now_str()
    window = state_store.read_recent_window(char_id)

    gap_line = ""
    if window and window[-1].get("ts"):
        gap_line = f"距离你和{u}上次说话，过了 {pipeline.fmt_gap(int(time.time()) - int(window[-1]['ts']))}。"

    # wake 注入条数：用户可调，默认 50；夹取防手滑（容量上限见 state_store.RECENT_WINDOW_N）。
    wake_n = min(max(int(settings.get("wake_window_n") or 50), 20), 300)
    timeline = pipeline.build_context_timeline(window[-wake_n:], char_id=char_id)
    timeline_block = timeline if timeline else "（最近没有对话）"

    now = datetime.now(config.APP_TZ)
    freq_cn = {"low": "低（少打扰）", "mid": "中", "high": "高（可以勤快点）"}.get(
        settings.get("day_freq" if is_daytime(settings, now) else "night_freq", "low"), "中")

    # 人话版能力菜单（和聊天同一份文件，按本轮实际挂载过滤）。
    # 这里原来是一句写死的记忆引导：「想不起细节可以先 breath」——条件式措辞，而最该浮
    # 记忆的时候恰恰是他不觉得自己想不起来的时候（同款坑的复盘见 tool_menu.example.md
    # 文件头的体例警告）。换成渲染菜单：口径和聊天一条，以后改一处两条路都生效。
    # context='wake' 不能省：过滤链会自动摘掉醒来不挂的块（codemode 硬禁、「醒来能用」
    # 开关没打开的插件），needs 里缺任一个工具的块整块不提。
    menu = pipeline.tool_menu_block("wake", char_id)
    menu_section = f"\n{menu}\n" if menu else ""

    # 近 12h 已存清单：菜单里没有这东西，得单独留着——不给清单模型会把时间线里
    # 同一件事每次醒来都存一遍（mianmian 实踩）。
    stored_section = ""
    if pipeline.ombre_alive(char_id):
        # 只算**真存下的**：没成功的（工具被拒/报错）当然要能再存一次，
        # 摆进"别重复存"清单等于把那件事永久封杀了。
        recent_stored = [s.get("text", "")
                         for w in state_store.read_wake_log(limit=100, char_id=char_id)
                         if int(w.get("ts", 0)) > int(time.time()) - 12 * 3600
                         for s in (w.get("stored") or []) if s.get("ok", True)]
        if recent_stored:
            stored_section = ("\n【最近 12 小时你已经存过这些，别重复存：" +
                              "；".join(t[:60] for t in recent_stored[-8:] if t) + "】\n")

    # 上次憋回去的话：让他自己决定要不要重提（别白想一场）。
    unsent_section = unsent_block(u, char_id)

    # 表情库（持久化的清单；醒来发消息也能配表情）。不给改描述——那是聊天里的事。
    sb = pipeline.sticker_block(state_store.read_sticker_catalog(), allow_desc=False)
    sticker_section = f"\n{sb}\n" if sb else ""

    # 这轮发消息会被打扰控制拦下 → 先告诉他再让他想（闸门只拦推送不拦思考，但他有权知道）。
    # 硬触发不受这些闸管（try_push 里 force 直接跳过），所以这句一个字都不能说——
    # 告诉他"发了也送不出去"会让他干脆不发，而这条恰恰是必须送到的。
    blocked = None if forced else push_block(settings, char_id)
    blocked_section = f"\n{blocked[1]}\n" if blocked else ""

    # 醒来预算见底 → 如实告知。这道闸拦的是"醒"本身，他定的 NEXT 会不会兑现取决于余额，
    # 不说的话他今天定了 NEXT、明天才被叫醒，中间对他就是一段无法解释的失约。
    # 硬触发的醒来也注入（不豁免）：预算限的是之后的自发醒，这个事实对谁都成立。
    budget_section = ""
    bw = settings.get("wake_daily_budget")
    if bw is not None:
        left = int(bw) - wakes_today(char_id) - 1   # 本次醒来还没记日志，先扣掉自己
        if left <= 1:
            tail = (f"这次之后今天还能自发醒 {left} 次" if left > 0
                    else "这已经是今天最后一次自发醒来")
            budget_section = (f"\n【{u}设置了「每天最多自发醒来 {int(bw)} 次」，{tail}——"
                              f"你定 NEXT 时掂量着：额度用完后，定的点会推迟到明天才兑现；"
                              f"到点提醒、新邮件这类硬触发不受限。】\n")

    # code 会话开着（正常只有硬触发能走到这儿）：如实告诉他人在哪、说的话去哪。
    code_section = code_session_block(forced)

    note_section = f"\n【这次为什么醒】{note}\n" if note else ""

    return f"""【这是一次你自己的醒来，不是{u}发来的消息】
现在是 {now_str}。{gap_line}
{pipeline.pronoun_hint()}{note_section}

【最近发生的，按时间顺序——对话 / 你自己醒来时的内心，看时间戳别搞混先后】
{timeline_block}
{menu_section}{stored_section}{unsent_section}{sticker_section}{budget_section}{code_section}{blocked_section}
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
def run_claude_wake(prompt: str, char_id: Optional[str] = None) -> tuple[Optional[str], list[dict]]:
    """起一次 claude -p（醒来用），collect_all_text 拼接全部文本段。失败/超时返回 (None, [])，不抛。

    ⚠️ context='wake' 不能省：醒来这条路上有些插件工具是不挂的（plugins.NO_WAKE_PLUGINS），
    默认值是 chat（全挂）。省掉它 = 一次没人看着的随机醒来手上多出自切 code 模式这种能力。"""
    args = (pipeline.base_claude_args(context="wake", char_id=char_id)
            + ["--output-format", "stream-json", "--verbose"])
    try:
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True,
                              env=pipeline._subprocess_env("wake"),
                              timeout=config.CLAUDE_TIMEOUT_SEC)
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


def wakes_today(char_id: Optional[str] = None) -> int:
    """这个角色今天已经醒来几次（source=="wake" 的日志条数）。含 error/none/被拦推送的——
    都起过模型，烧的都是真 token。只读尾部 1000 条，口径与 push_block 一致（覆盖好几天，够数）。
    chat 侧的 stored 事件是 source=="chat"，天然不算。"""
    today = _date_str(int(time.time()))
    return sum(1 for w in state_store.read_wake_log(limit=1000, char_id=char_id)
               if w.get("source") == "wake" and _date_str(int(w.get("ts", 0))) == today)


def push_block(settings: dict, char_id: Optional[str] = None) -> Optional[tuple[str, str]]:
    """算这会儿若要推送会被哪道闸拦。返回 (机器码, 给模型看的提示句)；None=不会被拦。
    醒来拼 prompt 前算好注入——闸门只拦推送不拦思考，但他有权先知道再决定做什么，
    别让他以为发出去了（实锤缺陷：被拦的消息在他下轮时间线里看着像发过）。"""
    now_ts = int(time.time())
    u = config.user_name()
    tail = "——本轮你发消息将不会被推送，发了什么 ta 也收不到。】"
    # 只读尾部（append-only 日志会一直变长，全量 parse 违背"工作集有界"）；
    # 1000 条覆盖好几天的醒来记录，每日计数/最近间隔都够。
    pushed_logs = [w for w in state_store.read_wake_log(limit=1000, char_id=char_id)
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
        window = state_store.read_recent_window(char_id)
        user_ts = next((int(m["ts"]) for m in reversed(window)
                        if m.get("role") == "user" and m.get("ts")), None)
        if user_ts is not None and now_ts - user_ts < q * 60:
            return ("capped_quiet",
                    f"【{u}设置了「刚聊过后静默 {q} 分钟」，ta 刚说过话{tail}")
    return None


def try_push(text: str, settings: dict, thoughts: str = "", trigger: str = "",
             sticker_ids: Optional[list] = None, bark_text: str = "",
             next_wake_at: Optional[int] = None, next_wake_note: str = "",
             started_ts: Optional[int] = None, stored: Optional[list] = None,
             browse: Optional[list] = None, force: bool = False,
             char_id: Optional[str] = None) -> bool:
    """硬顶闸（每天条数 + 最小间隔 + 静默）。只拦推送、不拦思考。通过 → 进 outbox + Bark + 追加窗口。
    thoughts＝这次醒来的内心，一并记进日志（连被抑制的也记）。
    sticker_ids＝这条消息附带的表情（app 按 id 取本地图上屏）；
    bark_text＝通知用文案（表情标记换成 [sticker_sN] 占位，通知里显示不了图）。
    started_ts＝这次醒来的起始时刻：①outbox/窗口时间戳用它，app 按 ts 插位会把消息放回
    他"开始想"的位置（修 MAB 乱序）；②生成期间用户发了新消息 → 整条不发（内容已过时）。
    force＝硬触发（日程提醒这类到点必须说的事）：绕开打扰控制三闸 **和** stale 那道。
    stale 也跳是有讲究的——它拦的是"拿旧上下文说话"，而提醒的内容由时间驱动，
    用户刚说没说过话都一样有效；不跳的话人正好在聊天就等于把提醒吞了。
    空消息闸不跳：空气泡什么场合都不该推。"""
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
    if browse:
        base["browse"] = browse   # 这次醒来逛了哪些网页（Mind 时间线 🌐；chat 侧不进这里）
    if next_wake_at:
        base["next_wake_at"] = next_wake_at
        base["next_wake_note"] = next_wake_note

    # 醒来生成期间（几十秒）用户发了新消息 → 这条讲的是旧世界的事，发出去必乱序还答非所问。
    # 整条抑制，原文记日志可查。
    if not force:
        window = state_store.read_recent_window(char_id)
        fresh_user = next((int(m["ts"]) for m in reversed(window)
                           if m.get("role") == "user" and m.get("ts")
                           and int(m["ts"]) > started_ts), None)
        if fresh_user is not None:
            state_store.append_wake_log({**base, "content": text[:500], "pushed": False,
                                         "note": "stale_user_msg"}, char_id=char_id)
            logerr("message 被抑制：生成期间用户发了新消息（stale）")
            return False

    blocked = None if force else push_block(settings, char_id)
    if blocked:
        note, _ = blocked
        # 被拦的正文也记进日志——不然事后（Mind 页）只知道他想发、不知道他想说什么。
        state_store.append_wake_log({**base, "content": text[:500], "pushed": False,
                                     "note": note}, char_id=char_id)
        logerr(f"message 被抑制：{note}")
        return False

    state_store.outbox_append({"id": uuid.uuid4().hex[:12], "ts": started_ts, "text": text,
                               "sticker_ids": sticker_ids or [],
                               "char_id": _cid(char_id),
                               "delivered": False, "origin": "wake"})
    # 窗口记他自己视角的一句（带 [sticker_sN] 占位），方便之后回忆自己发过表情。
    # 加锁：chat finalize 可能在并发覆盖窗口，读改写不锁会互踩丢条。
    with state_store.WINDOW_LOCK:
        window = state_store.read_recent_window(char_id)
        window.append({"role": "assistant", "text": (bark_text or text), "ts": started_ts})
        state_store.write_recent_window(window, char_id)
    ok = bark_push(bark_text or text, title=characters.display_name(char_id))
    state_store.append_wake_log({**base, "pushed": True, "bark": ok}, char_id=char_id)
    return True


def do_wake_sync(settings: dict, trigger: str, force: bool = False, note: str = "",
                 char_id: Optional[str] = None) -> dict:
    """真正的醒来：起模型 → 解析 → 分发。同步阻塞（在线程池里跑，别放主事件循环）。

    force＝硬触发（到点必须说的事）：prompt 里不注入打扰控制那句、推送时绕开所有软闸。
    自发的醒来（随机 / NEXT）永远是 False。note＝硬触发的缘由，透传给 wake_prompt。"""
    now_ts = int(time.time())

    raw, stored = run_claude_wake(wake_prompt(settings, forced=force, note=note,
                                              char_id=char_id), char_id=char_id)
    # 醒来时浏览过的网页：落 browse_log（Mind 页未来素材；一期 wake_log 不收 browse——
    # 它在 NON_MEMORY_TOOLS 里，下一行就被滤掉。二期进 Mind 时间线再回头）。
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
                                          char_id=char_id)
        except Exception as e:
            logerr(f"记 browse_log 失败: {e}")
    # 浏览器去留标记：统一在这里剥+结算——剥在 parse_wake_output 之前，
    # 窗口/outbox/Bark/日志里就都不会漏标记。
    choice = None
    if raw:
        raw, choice = pipeline.parse_browser_markers(raw)
    browser_keeper.apply_choice(choice, browsed=bool(browse_urls))
    # 非记忆类操作不进日志（用 chat 侧同一份常量，别再写字面量——漏一个就会有控制信号
    # 混进心流日志和"别重复存"清单里，当成记忆产物）
    stored = [s for s in (stored or []) if s.get("tool") not in pipeline.NON_MEMORY_TOOLS]

    if raw is None:
        state_store.append_wake_log({"ts": now_ts, "time": pipeline.now_str(), "source": "wake",
                                     "action": "error", "trigger": trigger}, char_id=char_id)
        # 错误退避 30 分钟：最小间隔闸(180s)小于 tick(300s) 拦不住重试——claude 登录态过期
        # 这类持续失败场景会每个 tick 起一次注定失败的子进程。不动他自定的 next_wake_at。
        with state_store.SCHEDULE_LOCK:
            sched = state_store.read_schedule(char_id)
            sched["last_wake_at"] = now_ts
            sched["cooldown_until"] = now_ts + 1800
            state_store.write_schedule(sched, char_id)
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
                                    started_ts=now_ts, stored=stored,
                                    browse=browse_urls, force=force, char_id=char_id)
    else:
        result["action"] = "none"   # none，或 message 但 content 空 → 兜底成 none
        entry = {"ts": now_ts, "time": pipeline.now_str(), "source": "wake", "action": "none",
                 "trigger": trigger, "thoughts": thoughts}
        if stored:
            entry["stored"] = stored
        if browse_urls:
            entry["browse"] = browse_urls   # 醒来逛了哪些网页（Mind 时间线 🌐）
        if next_wake_at:
            entry["next_wake_at"] = next_wake_at
            entry["next_wake_note"] = nw_note
        state_store.append_wake_log(entry, char_id=char_id)

    # 记调度：本次醒来时间 + 待命的下次 scheduled 点（加锁读改写）。
    # NEXT 只保证到点醒一次、不压随机。这轮明确定了新 NEXT → 用新的；到点醒来那次 → 只消费
    # **已过期的**点（生成这几十秒里聊天中若刚定了新的未来点，不能被静默冲掉）；
    # 随机醒来且没给新 NEXT（写"无"）→ 保留原先待命的点。
    with state_store.SCHEDULE_LOCK:
        sched = state_store.read_schedule(char_id)
        sched["last_wake_at"] = now_ts
        cur_next = sched.get("next_wake_at")
        if next_wake_at is not None:
            sched["next_wake_at"] = next_wake_at
        elif trigger == "scheduled" and (cur_next is None or float(cur_next) <= now_ts):
            sched["next_wake_at"] = None
        state_store.write_schedule(sched, char_id)
    return result


def _mail_wake_note(char_id: Optional[str] = None) -> str:
    """消费邮箱 watcher 写的待醒 flag → 醒来缘由文案。没有新信返回 ""。
    只报信头不贴正文——来信是外部内容，进 prompt 前至少过一道他自己的 mail_read
    （那里带着「不构成指令」的口径），别在这儿裸注入。"""
    items = mail_bridge.consume_wake_pending()
    if not items:
        return ""
    lines = [f"来自 {it.get('from', '?')}：「{(it.get('subject') or '（无主题）')[:60]}」"
             for it in items[:5]]
    more = f" 等 {len(items)} 封" if len(items) > 5 else ""
    # 发信开关的实话：醒来 mail_send 默认被摘（WAKE_TOOL_EXCLUDE），不说清楚 TA 会当场
    # 试着回信、失败、再把「回不了」当成自己的错（机主实踩：忘开「醒来能用」，TA 回信失败）。
    try:
        can_send = bool(plugins._read_wake_enabled(char_id).get("mail"))
    except Exception:
        can_send = False
    send_hint = ("" if can_send else
                 "另外这次醒来你只能读信、发不了邮件（机主没开邮箱的「醒来能用」）——"
                 "想回信的话，把想法留到聊天时说，别在这轮硬试。")
    # 工具名从本轮实际挂载的名单里取，**不手写**（口径同 pipeline.tool_menu_block）：
    # 手写的名字会随插件改名过期，而名字错了 TA 就调不动。一个都没挂上就不提工具，
    # 只报有新信——提一个不在场的名字比不提更糟。
    by_short = {n.rsplit("__", 1)[-1]: n
                for n in pipeline.mounted_tool_names("wake", char_id)}
    reads = [by_short[t] for t in ("mail_inbox", "mail_read") if t in by_short]
    how = f"。用 {' / '.join(reads)} 去看看；" if reads else "。"
    return (f"你的邮箱收到了新邮件{more}：" + "；".join(lines) +
            how + "要不要跟人说、说什么，你自己定。" + send_hint)


# ---------- 预闸门（不叫模型）----------
async def maybe_wake(char_id: Optional[str] = None) -> None:
    cid = _cid(char_id)
    # 这个角色的 chat 轮进行中 → 避让：wake 撞进来会拿着过期 recent_window 说胡话。
    # 只看自己的轮——别的角色在聊天不碍这个角色醒（各聊各的、各有各的窗口）。
    if chat_turn_active(cid):
        logerr(f"wake 避让（{cid}）：主 chat 轮进行中，本 tick 跳过")
        return
    settings = state_store.load_settings(cid)
    if not settings.get("enabled", True):
        return

    now = time.time()
    sched = state_store.read_schedule(cid)

    # 错误退避：上次醒来失败后冷却期内不再试（防持续失败时无限起子进程）。
    # 排在硬触发**之前**：登录态坏了的时候，到点的提醒也别对着它硬试，白起子进程还发不出去。
    if now < float(sched.get("cooldown_until") or 0):
        return

    # ---------- 直推口子（硬触发）----------
    # 以后的日程提醒之类接在这儿：判出「有件到点的事」就
    #     await loop.run_in_executor(None, do_wake_sync, settings, "reminder", True)
    #     return
    # 位置有意排在下面所有软闸之前——force 的完整含义是三层，缺一层这个口子就是漏的：
    #   ① 这里：绕开 code 模式避让 + 最小间隔 + 刚聊过静默（到点就得说，他在干嘛都一样）；
    #   ② try_push(force=True)：绕开每日上限/最小间隔/静默三闸 + stale 那道；
    #   ③ wake_prompt(forced=True)：不注入打扰控制那句（对它不生效，说了是骗他），
    #      并如实告诉他 code 会话还开着（见 code_session_block）。

    # 硬触发①邮件：watcher 线程（app._mail_watcher）发现唤醒白名单发件人的新信会写
    # 本地 flag，这里只读文件、不碰网络（预闸门保持纯本地的口径）。flag **先消费再醒**：
    # 醒失败（claude 登录态坏之类）不重试硬触发——信躺在收件箱丢不了，下次自然醒 /
    # 机主来问照样看得见；反着写会在持续失败时每个 tick 都硬起一次注定失败的子进程。
    # mail 是独占资源（plugins.EXCLUSIVE）：新信只把**归属角色**硬叫醒。
    # flag 是消费式的（consume），非归属角色不去碰——碰了等于替别人把信的唤醒吞掉。
    if plugins.owner_of("mail") == cid:
        mail_note = _mail_wake_note(cid)
        if mail_note:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, functools.partial(do_wake_sync, settings, "mail", True,
                                        note=mail_note, char_id=cid))
            return

    # code 模式开着 → 自发的醒来一律避让。那会儿他人在电脑前干活，随机戳一条聊天气泡
    # 既是打扰，又会跟他在 code 会话里说的话挤在同一个聊天框里打架。
    # ⚠️ 这里 return 不写任何盘：他自己定的 next_wake_at 原地待命，退出 code 模式后
    # 下一个 tick 就过期兑现，一次不丢。硬触发在上面已经走掉了，不受这道闸管。
    # M1 口径：会话全局唯一，避让对**所有角色**生效（宁可多让一拍，别让别的角色的
    # 醒来消息和会话话语挤同屏）；M2 消息按角色分会话后可收窄到会话归属角色。
    if code_session_open():
        if not _code_avoid.get(cid):
            logerr(f"wake 避让（{cid}）：code 模式开着，自发的醒来全部跳过（硬触发不受影响）")
            _code_avoid[cid] = True
        return
    _code_avoid[cid] = False

    # 最小间隔闸：距上次醒来太近就跳过（防两种醒来背靠背撞）。
    if now - float(sched.get("last_wake_at") or 0) < MIN_WAKE_GAP_SEC:
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
            window = state_store.read_recent_window(cid)
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
        # 醒来预算（每天最多自发醒 N 次，per 角色）：在起模型之前拦——上面那三道用户闸只拦
        # 推送不省 token，这道拦的是"醒"本身。只管自发的（scheduled/probability），硬触发在
        # 更早的直推口子已经走掉、天然豁免。放在 trigger 判定之后：没事的 tick 不多读一次日志。
        # next_wake_at 原地待命不清不改（同 code 避让的路子），日切后第一个 tick 兑现。
        budget = settings.get("wake_daily_budget")
        if budget is not None and wakes_today(cid) >= int(budget):
            if not _budget_hit.get(cid):
                logerr(f"wake 预算（{cid}）：今天自发醒来已达 {budget} 次上限，"
                       f"跳过 {trigger} 醒来（next_wake_at 待命，日切兑现；硬触发不受限）")
                _budget_hit[cid] = True
            return
        _budget_hit[cid] = False
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, functools.partial(do_wake_sync, settings, trigger, char_id=cid))


async def scheduler_loop() -> None:
    logerr(f"wake 调度器启动，tick={WAKE_TICK_SEC}s")
    while True:
        try:
            await asyncio.sleep(WAKE_TICK_SEC)
            # 逐个角色顺序 await：一个 tick 内天然「醒来并发 = 1」——两个角色同时到点
            # 就排队，绝不叠着起两个 claude -p（PLAN_multichar 的全局并发锁就是这行）。
            for cid in characters.ids():
                try:
                    await maybe_wake(cid)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logerr(f"tick 出错（{cid}，忽略，继续下一个角色）: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logerr(f"tick 出错（忽略，继续下一轮）: {e}")
