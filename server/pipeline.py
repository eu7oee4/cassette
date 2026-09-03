"""
claude -p 管道：拼 prompt → 一次性子进程 → stream-json 解析。

安全姿态（全仓硬约束）：
- 纯聊天 `--tools ""`；以后挂 MCP 工具时用 `--tools`+`--allowedTools` 精确白名单
  + `--strict-mcp-config`——headless 不弹权限靠预批准，绝不用 --dangerously-skip-permissions。
- 子进程 env 删掉 ANTHROPIC_API_KEY，凭据走 claude CLI 登录态（订阅），后端不碰 key。
- `--system-prompt-file` 是「替换」默认系统提示词，不是追加：模型只看到人设，干净。
  别改成 --append-system-prompt——那会把默认提示词整套灌进来，模型还会把人设当注入抵抗。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel
from typing import Literal

import config
import state_store
from notify import logerr


# ---------- 数据结构 ----------
class Message(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    ts: Optional[int] = None   # Unix 秒（app 发的消息时间）；不发也兼容


# ---------- 时间感知 ----------
# 数字时间会被长上下文淹没（实测：模型下午顺着聊天氛围说晚安），时段词免 24 小时制心算。
_WEEKDAYS_CN = ["一", "二", "三", "四", "五", "六", "日"]


def _daypart_cn(hour: int) -> str:
    if hour < 5:  return "凌晨"
    if hour < 8:  return "清晨"
    if hour < 11: return "上午"
    if hour < 13: return "中午"
    if hour < 17: return "下午"
    if hour < 19: return "傍晚"
    if hour < 23: return "晚上"
    return "深夜"


def now_str() -> str:
    """当前时间中文串，含星期和时段词（配置时区，各处共用）。"""
    now = datetime.now(config.APP_TZ)
    wd = _WEEKDAYS_CN[now.weekday()]
    return (f"{now.year}年{now.month:02d}月{now.day:02d}日 周{wd} "
            f"{now.hour:02d}:{now.minute:02d}（{_daypart_cn(now.hour)}）")


def fmt_ts(ts: int) -> str:
    """epoch → 'MM-dd HH:mm'（历史时间线用）。"""
    return datetime.fromtimestamp(int(ts), config.APP_TZ).strftime("%m-%d %H:%M")


def stamp_str(ts: int) -> str:
    """epoch → 'MM-dd 周X HH:mm'——**上下文里每条消息的时间锚**，全场一个格式。

    三处共用（2026-09-02 定的口径）：铸造侧给 TA 的话打戳（chat_loop._stamp_times）、
    醒来注入的抬头（wake_sdk.wake_injection）、游戏泵每 N 轮的报时。长得一样才认得出
    是同一种东西——重铸之后醒来那条注入只剩这个戳，它得跟活着时候的抬头对得上。

    比 now_str() 短：年份逐条重复零信息，时段词从 24 小时制直接读得出。星期留着——
    模型从日期反推星期不可靠，而"周末还是工作日"是他判断该不该打扰 TA 的依据。"""
    d = datetime.fromtimestamp(int(ts), config.APP_TZ)
    return f"{d.month:02d}-{d.day:02d} 周{_WEEKDAYS_CN[d.weekday()]} {d.hour:02d}:{d.minute:02d}"


def fmt_gap(seconds: int) -> str:
    """秒差转人话：不到1分钟 / N分钟 / N小时 / N天。"""
    if seconds < 60:
        return "不到 1 分钟"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时"
    return f"{hours // 24} 天"


def gap_before_last(messages: list[Message]) -> Optional[str]:
    """最新消息与上一条的时间间隔（人话）。任一条缺 ts 则 None。"""
    if len(messages) < 2:
        return None
    last, prev = messages[-1], messages[-2]
    if last.ts is None or prev.ts is None or last.ts < prev.ts:
        return None
    return fmt_gap(last.ts - prev.ts)


# ---------- 上下文时间线 ----------
def build_context_timeline(conv_items: list[dict], reflect_limit: int = 5,
                           char_id: Optional[str] = None,
                           experience_limit: int = 0) -> str:
    """把 最近对话 + 模型自己醒来时的内心（不在聊天里，用户在心流日志页看得到）合并成
    一条按时间排序的时间线。
    醒来和聊天共用——解决「对话本身没时间戳、和内心对不上先后顺序」的问题。

    ⚠️ char_id 不能省：内心来自 wake_log，而 wake_log 是**按角色分文件**的。不传就永远
    读默认角色那份——对话是 A 的、内心是 B 的，A 会读到一段自己没想过的心事然后接着往下说。
    只有一个角色时看不出来（默认角色恰好就是自己），第二个角色一进来就串。

    experience_limit > 0 时再并入第三路：小屋经历流（他在场看见的房间事件，跨房间、
    跨在场区间，world._append_experience 在发生那一刻定格的）——三路合出「第一人称
    经历时间线」（PLAN_cohabit 定稿）。聊天路和同居醒来路都传 world.experience_limit()
    （小屋级旋钮，默认 40；2026-08-16 起两路同构，现场段只留快照不再单列事件）。"""
    items: list[tuple[int, str]] = []

    for c in conv_items:
        ts = c.get("ts")
        if ts is None:
            continue
        who = config.user_name() if c.get("role") == "user" else "你"
        items.append((int(ts), f"{who}：{c.get('text', '')}"))

    items += seen_items(char_id, reflect_limit=reflect_limit,
                        experience_limit=experience_limit)
    items.sort(key=lambda x: x[0])
    return "\n".join(f"[{fmt_ts(ts)}] {txt}" for ts, txt in items)


def seen_items(char_id: Optional[str], reflect_limit: int = 5,
               experience_limit: int = 0, since_ts: int = 0) -> list[tuple[int, str]]:
    """时间线里**非对话**的两路（醒来内心 + 小屋经历），按时间升序。
    build_context_timeline 与 SDK 聊天路（chat_loop 的见闻增量注入）共用这一份
    渲染口径——别再各写一套。since_ts>0 = 只要这之后新长出来的（增量注入用）。"""
    items: list[tuple[int, str]] = []

    # 最近几次醒来的内心（含 none；不在聊天里，心流日志页可见）。只读日志尾部——append-only 文件会一直长。
    # engine=sdk 的醒来（PR12 起在聊天 session 里发生）整个跳过：内心已经活在
    # transcript 里，再从这儿注回去=①同一句在场两份；②重铸后本该淡忘的闲念头
    # 变成档案句复活——正是「推断回灌固化成事实」的载体（08-30 Cassius「你昨晚
    # 五点睡的」实锤：05:05 醒来的一句猜测经 12:08/20:41 两轮回灌变成他笃信的
    # 事实，还碰巧蒙对）。老 -p 路的醒来照旧注入（那条路没有活着的 transcript）。
    for w in [e for e in state_store.read_wake_log(limit=100, char_id=char_id)
              if (e.get("thoughts") or "").strip()
              and e.get("engine") != "sdk"][-reflect_limit:]:
        if int(w.get("ts", 0)) <= since_ts:
            continue
        act = {"none": "没做什么", "message": "发了消息"}.get(w.get("action"), "")
        # 被打扰控制拦下的消息：标清楚没送出去，别让他以为发过了接着那条往下聊。
        if w.get("action") == "message" and not w.get("pushed"):
            act = "想发消息但被打扰控制拦下、没送出去"
        th = (w.get("thoughts") or "").strip().replace("\n", " ")
        if len(th) > 140:
            th = th[:140] + "…"
        # 「内心/你想」的措辞本身就表达了"没说出口"，不用额外标注可见性。
        items.append((int(w.get("ts", 0)), f"〔你醒来·{act}〕你想：{th}"))

    # 第三路：小屋经历流（带（房间名）前缀，和手机消息一眼分得开）。
    if experience_limit and char_id:
        import world
        reg = world.load_registry()
        for ev in world.read_experience(char_id, limit=experience_limit):
            if int(ev.get("ts", 0)) <= since_ts:
                continue
            rn = (reg.get(ev.get("room", "")) or {}).get("name") or ev.get("room", "?")
            actor = ev.get("actor", "")
            name = "你" if actor == char_id else world.entity_name(actor)
            t = ev.get("type")
            if t == "speech":
                line = f"（{rn}）{name}：「{world.strip_quotes(ev.get('text', ''))}」"
            elif t == "action":
                line = f"（{rn}）{name} *{ev.get('text', '')}*"
            else:
                line = f"（{rn}·{ev.get('text', '')}）"
            items.append((int(ev.get("ts", 0)), line))

    items.sort(key=lambda x: x[0])
    return items


# ---------- 表情包 ----------
# 统一用「朴素 dict 清单」(catalog)：[{id, description, num}]。
# 聊天时 app 传清单 → 转成 catalog；醒来时 app 不在场 → 读持久化的 catalog。
# 代号 s{num} 用 app 侧的永久序号（删表情不错位），标记从不落库所以改格式不用迁移。
def to_catalog(stickers) -> list[dict]:
    """StickerInfo 列表（或已是 dict）→ 朴素 dict 清单，供 build_prompt / 持久化 / 醒来复用。"""
    out = []
    for s in (stickers or []):
        if isinstance(s, dict):
            out.append({"id": s.get("id"), "description": s.get("description", ""), "num": s.get("num")})
        else:
            out.append({"id": s.id, "description": s.description, "num": s.num})
    return out


def _sticker_handle(i: int, s: dict) -> str:
    """代号：优先用 app 给的永久序号 s{num}（稳定）；没有则退回按位置 s{i+1}。"""
    num = s.get("num")
    return f"s{num}" if num is not None else f"s{i + 1}"


def sticker_handle_map(catalog) -> dict:
    """返回 {代号: sticker_id}。代号用永久序号，删表情不会错位。"""
    return {_sticker_handle(i, s): s["id"] for i, s in enumerate(catalog or [])}


def sticker_block(catalog, allow_desc: bool = True) -> str:
    """表情库清单 + 用法，注入提示词。空则返回空串。allow_desc=False 时不提供改描述（醒来用）。"""
    if not catalog:
        return ""
    lines = [f"【你有这些表情包，可以在合适的时候发给{config.user_name()}——别滥发，偶尔、贴当下情绪才发】"]
    for i, s in enumerate(catalog):
        desc = (s.get("description") or "").strip() or "（还没有描述）"
        lines.append(f"{_sticker_handle(i, s)}：{desc}")
    lines.append("想发就在回复里写 [[sticker:s1]]，可以和文字一起（这个标记会被替换成表情图，对方只看到图）。")
    if allow_desc:
        # 例子用不存在的序号（sN 不在 handle_to_id 里 → 复述也改不动）：这条会**改盘**
        # （写 catalog 描述）且静默。发表情那条留真值 s1——后果只是多发一张表情，
        # 聊天里看得见、也就撤得掉。判据见 PLAN_native §14.0 规避规则 3。
        lines.append("如果你觉得某张的描述不准，可以顺手改：[[sticker_desc:sN=新的描述]]。")
    return "\n".join(lines)


# ---------- 引用逃逸（2026-09-02 事故，复盘全文 PLAN_native §14.0）----------
# 「**提及即使用**」：模型在正文里**复述**一段带标记的文本（贴注入原文、解释用法、
# 跟机主讨论这个机制），解析器分不出「他在下指令」和「他在引用」，照样执行。
# 09-02 22:46 实锤：机主问小卡「你现在看到的首轮长什么样」，她用 ``` 围栏把注入
# 原样贴出来，里面 one_turn_hint 的例子 [[next_wake:1小时|…]] 被 parse_chat_next
# 执行、顶掉了她自己 00:50 的钟；strip_markers 又把它剥掉，机主手机上只看到一段
# 带窟窿的话——窟窿本身不解释自己，没人当场发现。
#
# 所有 [[…]] 解析共用这一份：落在代码块/行内 code 里的标记**既不执行、也不剥**。
# ⚠️ 不剥是要点，不是顺手：剥了就又是「带窟窿的正文」，而那正是这次没被发现的原因。
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def quoted_spans(text: str) -> list[tuple[int, int]]:
    """正文里「引用区」的字符区间：``` 围栏（没闭合就吃到结尾）+ 行内 code。
    围栏先算，行内 code 只在围栏外找——围栏里的单个反引号不该切出区间。"""
    spans = [m.span() for m in _FENCE_RE.finditer(text)]
    for m in _INLINE_CODE_RE.finditer(text):
        if not any(a <= m.start() < b for a, b in spans):
            spans.append(m.span())
    return spans


def sub_outside_quotes(rx: "re.Pattern", repl, text: str) -> str:
    """`rx.sub(repl, text)`，但跳过引用区里的匹配（原样留着）。
    副作用都写在 repl 里，所以「不调 repl」＝「不执行」，两件事同一个开关。"""
    if not text or "[[" not in text:
        return text
    spans = quoted_spans(text)
    if not spans:
        return rx.sub(repl, text)

    def _guard(m):
        if any(a <= m.start() < b for a, b in spans):
            return m.group(0)
        return repl(m)

    return rx.sub(_guard, text)


# 标记统一用英文 token（模型更不易写歪）；解析端兼容旧中文写法 + 全角冒号 + 空格做容错。
# 顺序上 DESC 先匹配（含 desc/描述），SEND 再匹配，互不误吞。
_STICKER_SEND_RE = re.compile(r"\[\[\s*(?:sticker|表情包?)\s*[:：]\s*(s\d+)\s*\]\]", re.I)
_STICKER_DESC_RE = re.compile(r"\[\[\s*(?:sticker[_\- ]?desc|表情包?描述)\s*[:：]\s*(s\d+)\s*[=＝](.*?)\]\]", re.S | re.I)


def parse_sticker_markers(reply: str, handle_to_id: dict) -> tuple[str, list, list]:
    """从回复里解析并剥掉表情标记，返回 (清理后的文本, [要发的id], [{id,description}])。"""
    sends: list = []
    updates: list = []

    def on_desc(m):
        h, d = m.group(1), m.group(2).strip()
        if h in handle_to_id and d:
            updates.append({"id": handle_to_id[h], "description": d})
        return ""

    def on_send(m):
        h = m.group(1)
        if h in handle_to_id:
            sends.append(handle_to_id[h])
        return ""

    reply = sub_outside_quotes(_STICKER_DESC_RE, on_desc, reply)
    reply = sub_outside_quotes(_STICKER_SEND_RE, on_send, reply)
    return reply.strip(), sends, updates


def split_wake_stickers(content: str, handle_to_id: dict) -> tuple[str, list, str]:
    """醒来时消息里的 [[sticker:sN]] 一分为三：
      - app_text ：剥掉标记的纯文字（app 里表情单独当图片消息渲染）
      - ids      ：[sticker_id...]（app 按 id 取本地图上屏）
      - bark_text：标记换成 [sticker_sN]（推送通知里没法显示图，用代号占位）"""
    ids: list = []

    def on_app(m):
        h = m.group(1)
        if h in handle_to_id:
            ids.append(handle_to_id[h])
        return ""

    app_text = _STICKER_SEND_RE.sub(on_app, content).strip()
    bark_text = _STICKER_SEND_RE.sub(lambda m: f"[sticker_{m.group(1)}]", content).strip()
    return app_text, ids, bark_text


# ---------- 聊天里定下次醒来 ----------
# 范围口径三处一致（聊天提示词 / 醒来提示词 / 解析夹取）。
NEXT_MIN_MIN = 5     # 模型自定下次醒来的下限（分钟）
NEXT_MAX_MIN = 720   # 上限 12 小时

_CHAT_NEXT_RE = re.compile(r"\[\[\s*(?:next[_\- ]?wake|下次醒来)\s*[:：]\s*(.*?)\]\]", re.I)

# NEXT 里的待办：时间和「下一轮要做什么」用竖线隔开（[[next_wake:1小时|给安瞬回信]]）。
# 全角｜一起收——中文输入法下打出全角是常态，不收的话整段会被当成时间去解析。
_NEXT_TODO_SEP = re.compile(r"\s*[|｜]\s*")
NEXT_TODO_MAX = 200   # 待办存进 schedule 的长度上限（防模型把整段计划塞进来）


def split_next_raw(raw: str) -> tuple[str, str]:
    """把 NEXT/[[next_wake]] 的原话切成 (时间部分, 待办)。没写待办 → 待办是空串。
    只切第一个竖线：待办正文里再出现竖线是它自己的事。"""
    if not raw:
        return "", ""
    parts = _NEXT_TODO_SEP.split(raw.strip(), maxsplit=1)
    return parts[0].strip(), (parts[1].strip()[:NEXT_TODO_MAX] if len(parts) > 1 else "")


def pronoun_hint(second_person: bool = True) -> str:
    """人称提示（聊天/醒来/-p 共用注入）。两个轴，别混：

    - **代词性别**（她/他/TA）：不给的话模型会自己猜用户性别，猜错很伤。
    - **人称视角**（second_person，08-31 加）：说出去的话一律第二人称。判据跟
      醒来契约的〔〕边界是同一条——〔〕外＝送到 TA 眼前的话，〔〕内＝只留档的
      独白。实踩：08-31 事故那轮开口就是「**她**点头了，我发。」——在对着 TA
      说话的轮里滑进了旁白口吻，而旁白里「我发」是句舞台指示、不是承诺，紧接着
      就编了一次没发生的投递。

    second_person=False 给同居世界（cohabit）：那条路上他可能在跟别人说话、或者
    在描述屋里的事，第三人称说 TA 是对的（机主 08-31 拍板：那条不管）。"""
    u, p = config.user_name(), config.user_pronoun()
    head = f"【提到{u}时，人称代词一律用「{p}」——写记忆、内心独白也一样。"
    if not second_person:
        return head + "】"
    return (head + f"\n但**说给{u}听的每一句，都用「你」**：会送到{u}眼前的字就是"
            f"当面说的话，里面出现「{p}」，等于当着{u}的面把{u}说成第三个人。\n"
            "第三人称只属于两个地方——**存进记忆的**，和**〔〕包起来的心里话**。】")


def _chat_next_hint() -> str:
    # 函数不是模块级常量：名字用户随时可改，import 时冻结就换不动了。
    # 口径必须和 one_turn_hint('chat') 对得上：那条规矩的②就是靠这个标记落地的，
    # 两处说法不一样，模型会照着更近的那条写。
    return (f"【可选：如果{config.user_name()}提到要离开/回来/睡觉之类，你可以顺手安排下次主动醒来——"
            "在回复里写 [[next_wake:多久后]]（范围 5 分钟~12 小时，会被剥掉、对方看不到）；"
            "要给下一轮留活就写 [[next_wake:多久后|下一轮要做什么]]。没必要就别写。】")


def parse_next_minutes(section: str) -> Optional[int]:
    """把「90分钟」「3小时」这类字样解析成分钟数，夹在 [5, 720]。'无'/空/解析不出 → None。
    ⚠️ 先把待办切掉再解析——「无 | 读第 2 封信」这种写法里，兜底的"有数字就当分钟"
    会去咬待办里的数字，把"不定点"读成"2 分钟后醒"。所有调用方都靠这一句挡着。"""
    s = split_next_raw(section)[0]
    if not s or s in ("无", "None", "none", "-"):
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*(小时|时|h|hour|hr)", s, re.I)
    if m:
        mins = float(m.group(1)) * 60
    else:
        m = re.search(r"(\d+(?:\.\d+)?)", s)   # 有数字就当分钟
        if not m:
            return None
        mins = float(m.group(1))
    return max(NEXT_MIN_MIN, min(NEXT_MAX_MIN, int(round(mins))))


def parse_chat_next(reply: str) -> tuple[str, Optional[int], Optional[str], str]:
    """从聊天回复里解析并剥掉 [[next_wake:X|待办]]，返回
    (清理后文本, 分钟或None, 时间原话或None, 待办)。取最后一个有效值。
    待办只跟着有效时间走：没解析出时间的标记整条作废，待办也跟着丢——
    留一个没有兑现时点的待办，等于永远注入、永远清不掉。"""
    found: list = []   # [(分钟, 时间原话, 待办)]

    def on_match(m):
        raw = m.group(1).strip()
        mins = parse_next_minutes(raw)
        if mins is not None:
            head, todo = split_next_raw(raw)
            found.append((mins, head, todo))
        return ""

    reply = sub_outside_quotes(_CHAT_NEXT_RE, on_match, reply)
    if found:
        mins, head, todo = found[-1]
        return reply.strip(), mins, head, todo
    return reply.strip(), None, None, ""


def next_wake_note(raw: str, at: int) -> str:
    """定了下次醒来的提示文案：原话(相对) + 夹取后的绝对时间点。聊天灰字用。
    只放时间部分——待办是他给自己留的话，不往用户那边灰字里漏。
    措辞按小字提醒的口吻（PLAN_chatui §3.4：`✦ Cassius 决定下次21:13醒来`——
    app 端把角色名接在前面，这里从「决定」说起）。"""
    return f"决定下次 {fmt_ts(at)} 醒来（{raw}）"


# ---------- 「你只有这一轮」 ----------
# 一次性子进程的硬事实：正文吐完进程就退，没有"说完之后"。模型不知道这件事，于是会写
# 「我现在就去回信」「名册我一并更新」——这些话在它这儿全是空头，用户却当承诺听。
# 实锤（2026-08-28 21:49）：同一条回复里，开口前做的（读完十封信、搭出名册网页）都成了，
# 放到开口后的（回信、更新名册）一件没做，且既没 hold 记忆也没定 next_wake——
# 意图只以正文形式活在 recent_window 里，没有任何执行钩子。
# 所以规矩是二选一，不是提醒："先做后说" 或 "钉到下一轮"，两个都不选就别说出口。
# 复盘全文（含归类、规避规则、清除口径三种情形）在主仓 PLAN_oneturn.md。
# ⚠️ 这段被淹掉就等于没有：它排在 prompt 尾巴上是有意的，别挪回 extras（见 build_prompt）。
def one_turn_hint(kind: str = "chat") -> str:
    """禁空头承诺的规矩，按场景分三个变体（机主 08-30 拍板：存在论解释全删，
    只写机制）。kind='chat'/'wake' → ①先做后说 ②钉下一轮 两条，定点写法分叉
    （marker vs NEXT 段）；'chat_session' → SDK 常驻路（PLAN_sdk PR10），
    只留 next_wake 用法一句。"""
    # ⚠️ 例子一律写成**解析不出来**的占位形式（「多久后」不含数字 → parse_next_minutes
    # 返 None → 整条作废），别写真值。2026-09-02 实锤：这里原来的「例：[[next_wake:1小时|
    # 给安瞬回信，回完更新名册]]」被复述了一次，当场改掉了她自己的钟。写在 prompt 里的
    # 可执行例子＝埋在文档里的地雷，引用逃逸只是第二道闸。全文 PLAN_native §14.0。
    if kind == "wake":
        how = ("在 NEXT 那段写成「时间 | 下一轮要做什么」，例：\n"
               "   NEXT: 多久后 | 到时候要做什么")
    else:
        how = ("在回复里写 [[next_wake:多久后|下一轮要做什么]]（会被剥掉、对方看不到）")
    if kind == "chat_session":
        # 机主拍板（08-30）：session 路把「这轮那轮」的解释整段删掉，只留用法——
        # 存在感的事不解释，工具的事才写字。
        return ("【想留到之后做的事，写 [[next_wake:多久后|要做什么]]（会被剥掉、对方看不到）。"
                "竖线后那句会原样存下来，到点递回给你——写清楚做什么，别写「继续」；"
                "不带时间就没有钉子。】")
    # 机主拍板（08-30）：「你只有这一轮/进程结束」的存在论开场白全线删掉，
    # 只留机制。禁空头承诺的规矩还在——就是下面这两条本身。
    return ("【要提一件还没做的事，二选一：\n"
            "① 这一轮里就做掉，做完了再开口——工具都在你手上，回复里报结果，别报打算；\n"
            f"② 现在做不完、或者现在不该做 → 钉到下一轮：{how}\n"
            "   竖线后面那句会原样存下来，到点醒来时递回给你，所以写清楚做什么，别写「继续」。"
            "没有时间就没有钉子——②必须带时间。\n"
            "两个都不选，就别把这件事说出口。】")


def pending_todo_block(char_id: Optional[str] = None, kind: str = "chat") -> str:
    """现在钉着的那个钟 + 上一轮给自己留的活（schedule 的 next_wake_at/_todo）。都没有则空串。
    kind 只管定点写法分叉（'wake'＝老路的 NEXT 段，其余＝[[next_wake:]] 标记），口径同 one_turn_hint。
    读盘失败一律当没有：这是个提醒块，为它把一轮聊天/醒来搞崩不值。

    两种形态，按「到点没到」分：
    · **还没到点**（聊天里看见它）：报钟点，并说清再写一次定点是**把这个钟挪走**、不是另加一个。
      2026-09-01 补的，欠的账是这次：08-31 20:59 他自己钉了 22:59 的钟，21:24 在聊天里
      又写了一句 [[next_wake:8小时]]——schedule 只有一个 next_wake_at 槽，
      finalize_chat_reply 无条件覆盖，22:59 当场作废，从外面看像「闹钟没响」。
      在此之前这块只递「留的活」不递钟点：钟没配待办时整块都不出现，他重钉的时候
      手上一个字都没有，等于闭着眼睛顶掉自己的承诺。
    · **已经到点**（scheduled 醒来那轮走到这儿，钟还没被 finish_wake_turn 消费）：原文照旧，
      现在就是那个「下一轮」。没留活就不说话——一个空钟没什么可交代的。"""
    try:
        sched = state_store.read_schedule(char_id)
        todo = (sched.get("next_wake_todo") or "").strip()
        at = sched.get("next_wake_at")
        at = float(at) if at is not None else None
    except Exception:
        return ""

    if at is not None and at > time.time():
        how = "在 NEXT 那段重写一个时间" if kind == "wake" else "写 [[next_wake:…]]"
        # 「N 后」不写「还有 N」：fmt_gap 最小档是「不到 1 分钟」，拼成「还有 不到 1 分钟」难看。
        head = f"【你现在钉着一个钟：{fmt_ts(int(at))}（{fmt_gap(int(at - time.time()))}后）"
        head += f"，到点要做的是：「{todo}」\n" if todo else "，没配待办。\n"
        return (head +
                f"钟只有一个：这一轮再{how}，是把这个钟**挪到新时间**，不是另加一个闹钟。"
                "想留着原来那个点，这轮就别再定；要改就当成改钟来写（连带留的活一并写全）。】")

    if not todo:
        return ""
    return (f"【你上一轮给自己留了活：「{todo}」\n"
            "现在就是那个「下一轮」。要么这一轮里做掉，要么重新钉一次（写清还剩什么没做）；"
            "不想做了就明说一句，别默默留着——留着它下一轮还会再递给你。】")


# 聊天回复附带的移动（同居世界 C2）：[[move:房间id]]。英文 token 为主（§4 口径），
# 容错中文写法和全角冒号。房间 id 是注册表键（ascii），不用管中文转义。
_CHAT_MOVE_RE = re.compile(r"\[\[\s*(?:move|移动|去)\s*[:：]\s*([A-Za-z0-9_]+)\s*\]\]", re.I)


def parse_chat_move(reply: str) -> tuple[str, Optional[str]]:
    """从聊天回复里解析并剥掉 [[move:X]]，返回 (清理后文本, 目的地或None)。取最后一个。
    目的地是否真的存在这里不管——执行方（cohabit_queue.chat_move）过注册表和门禁。"""
    found: list[str] = []

    def on_match(m):
        found.append(m.group(1).strip())
        return ""

    reply = sub_outside_quotes(_CHAT_MOVE_RE, on_match, reply)
    return reply.strip(), (found[-1] if found else None)


# ---------- 缓存断点 ----------
# prompt 的稳定段（能力菜单）单独切成一个 content block 打上 cache_control，让它跨轮命中
# 缓存，不再每轮重建。实测（2026-08-27，聊天配置 83 个工具）：稳定块 7,657 token，
# 第二轮 cache_read 命中、cache_creation 从 9,758 掉到 60。
#
# ⚠️ ttl 必须写 "1h"。CLI 自己那三个断点（tools / system / 消息末尾，都已实测定位过）
# 全是 1h，而 API 规定 1h 的块不能排在 5m 的块后面——用默认 5m 会直接 400，且报错文案
# 只说排序不说 ttl，很难往这儿想。
#
# ⚠️ 名额只有一个。API 上限是 4 个 cache_control 块，CLI 占了 3 个（`--tools ""` 也照占：
# 断点标记跟里面装没装东西无关）。这里**只能打一个**，多打一个就是
# "A maximum of 4 blocks with cache_control may be provided. Found 5." 的 400。
# 也就是说 CLI 哪天升级自己多打一个，我们这个断点会把每一轮都顶成 400 ——
# 所以下面那道自动降级不是锦上添花，是这刀敢上线的前提。
CACHE_BP_TTL = "1h"
_BP_LIMIT_RE = re.compile(r"maximum of \d+ blocks with cache_control", re.I)
_bp_enabled = True


def cache_bp_on() -> bool:
    return _bp_enabled


def disable_cache_bp(where: str) -> None:
    """断点名额被 CLI 吃满了 → 本进程之后一律不打断点。
    降级之后只是变慢变贵，不再报错——**所以必须往日志里喊**，否则这事没有任何症状。"""
    global _bp_enabled
    if _bp_enabled:
        _bp_enabled = False
        logerr(f"缓存断点被拒（{where}）：claude CLI 自己的 cache_control 块变多了，"
               f"本进程起不再打断点。prompt 缓存失效、聊天和醒来会变慢变贵——查 CLI 版本。")


def bp_limit_hit(text: str) -> bool:
    """这坨 stdout / 这条 result 文案是不是「断点超限」那个 400。"""
    return bool(text and _BP_LIMIT_RE.search(str(text)))


class SplitPrompt(str):
    """带缓存切点的 prompt。**本体就是完整字符串**——所有老调用点、日志、测试断言一个字
    都不用改，只有 stdin_payload 会去看 cut。

    cut = 稳定段的长度：[:cut] 逐轮不变（能力菜单），[cut:] 每轮都变（时间线/时间/新消息）。

    切点为什么只能切在菜单后面：时间线是**滑动窗口**（app 只发最近 sendHistoryCap 条），
    每轮一问一答两条，窗口起点跟着往后挪——切在它后面前缀每轮都不一样，缓存永远不命中。
    要把时间线也纳进来，得先把窗口锚定住（起点和切点都吸附到格子上），那是另一刀。

    ⚠️ 切片/拼接会退化成普通 str（cut 丢失）——要改内容就重新构造一个，别在中途变换。"""
    cut: int

    def __new__(cls, stable: str, volatile: str):
        o = super().__new__(cls, stable + volatile)
        o.cut = len(stable)
        return o


# ---------- prompt ----------
def build_prompt(messages: list[Message], catalog: Optional[list[dict]] = None,
                 char_id: Optional[str] = None,
                 extra_hints: Optional[list[str]] = None) -> str:
    """把 app 传来的完整历史拼成一次性提示词。人设在系统提示词里，这里只有对话本身。
    时间感（当前时间+时段词、距上一条的间隔）注入在**末尾、紧贴新消息**——放顶部会被
    长对话淹掉，prompt 末尾是 recency 权重最高的位置。恒为 1~2 行、不随历史增长。
    extra_hints＝调用方按场景附加的提示段（如同居世界的 [[move:]] 提示），排进 extras。

    返回 SplitPrompt：本体是完整字符串（调用方当普通 str 用就行），额外带一个缓存切点，
    切在能力菜单之后——菜单前面那段逐轮不变，能当缓存前缀。"""
    *history, last = messages

    time_lines = [f"【现在是 {now_str()}】"]
    gap = gap_before_last(messages)
    if gap:
        time_lines.append(f"【距离上一条消息，过了 {gap}】")

    # 能力菜单取代了原来写死的 memory_block（内容搬进 tool_menu.example.md）：
    # 一份可编辑的文件、按本轮实际挂载过滤，聊天和醒来共用同一份来源。
    # 位置从 extras 中间提到**整段最前面**：它是这份 prompt 里唯一逐轮不变的大块，
    # 只有排在最前才当得了缓存前缀（见 SplitPrompt）。菜单是参考资料不是叮嘱，
    # 放最前不吃 recency 的亏。
    mb = tool_menu_block("chat", char_id)
    stable = f"{mb}\n\n" if mb else ""

    extras = [pronoun_hint(), _chat_next_hint()] + [h for h in (extra_hints or []) if h]
    sb = sticker_block(catalog)
    if sb:
        extras += ["", sb]

    # 「你只有这一轮」和上一轮留的活排在**尾巴上**，不进 extras：extras 在整段最前面，
    # 长对话一堆时间线压下来就淹了，而这条恰恰是被淹掉才出事的那条（见 one_turn_hint 的
    # 复盘）。放在时间感之前——时间感仍然贴着新消息，那句注释的口径不动。
    tail_rules = [one_turn_hint("chat")]
    pending = pending_todo_block(char_id)
    if pending:
        tail_rules.append(pending)

    if not history:
        return SplitPrompt(stable, "\n".join(extras + [""] + tail_rules + [""]
                                             + time_lines + ["", last.text]))

    lines = extras + [""]
    # 合并时间线：历史对话 + 醒来内心 + 小屋经历流（同居开着时），按时间排。
    conv_items = [{"ts": m.ts, "role": m.role, "text": m.text} for m in history]
    import world   # 延迟导入，同 build_context_timeline（防循环）
    house_on = world.house_active()   # 总开关热读：休眠中经历流不再并入（没有现场）
    exp_n = world.experience_limit() if house_on else 0
    timeline = build_context_timeline(conv_items, char_id=char_id,
                                      experience_limit=exp_n)
    if timeline:
        if house_on:
            lines.append("【下面是最近发生的，按时间顺序——手机对话 / 你醒来时的内心 / "
                         "你在小屋里看见的（带（房间名）前缀），看时间戳别搞混先后】")
        else:
            lines.append("【下面是最近发生的，按时间顺序——对话 / 你自己醒来时的内心，看时间戳别搞混】")
        lines.append(timeline)
    else:
        # 历史全缺 ts（老客户端）：退回朴素列表
        lines.append("【下面是你们最近的对话，按时间顺序】")
        for m in history:
            who = config.user_name() if m.role == "user" else "你"
            lines.append(f"{who}：{m.text}")
    lines.append("")
    lines.extend(tail_rules)
    lines.append("")
    lines.extend(time_lines)   # 时间感贴着新消息，别被上面的长对话淹掉
    lines.append("")
    lines.append("【回下面这条。按这句的份量和情绪回：随口就随口，别硬凑长，一句话或一个词也可以。】")
    lines.append(f"{config.user_name()}：{last.text}")
    return SplitPrompt(stable, "\n".join(lines))


# ---------- 人设渲染 ----------
def rendered_persona(char_id: Optional[str] = None) -> Path:
    """人设文件支持 {{AGENT_NAME}} / {{USER_NAME}} 占位符（角色名在 .env 配，不用改文件）。
    每次调用现渲染 → 保持「改人设不用重启」的热读语义；没用占位符就原文件直传，零开销。
    多角色：人设来源和渲染产物都按角色走（characters.persona_path / 角色 state 目录）。"""
    import characters
    raw = characters.persona_path(char_id).read_text("utf-8")
    out = (raw.replace("{{AGENT_NAME}}", characters.display_name(char_id))
              .replace("{{USER_NAME}}", config.user_name()))
    if out == raw:
        return characters.persona_path(char_id)
    path = state_store.char_state_dir(char_id) / "persona_rendered.md"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(out, "utf-8")
    tmp.replace(path)   # 原子替换：并发请求各写各的 tmp，不会互相踩
    return path


# ---------- 长期记忆 Ombre-Brain ----------
# P0luz 的开源项目（https://github.com/P0luz/Ombre-Brain），自部署服务，只对接不 vendor。
# 白名单 = Ombre 的全部记忆工具：全是"他自己的记忆"域内操作，记错后果轻、用户可在
# Dashboard 删改；内置危险工具（Bash/Write 等）依旧完全不进来，安全姿态不变。
OMBRE_TOOLS = [f"mcp__ombre-brain__{t}" for t in (
    "breath", "breath_search", "breath_advanced", "hold", "grow", "trace",
    "source_read", "dream", "anchor", "release", "pulse", "plan",
    "letter_write", "letter_read", "I",
)]
_OMBRE_PROBE_TIMEOUT = 1.5    # 探活短超时：Ombre 挂了最多拖慢一次请求这么点
_OMBRE_PROBE_CACHE_SEC = 30   # 探活结果缓存，别每条消息都开一次连接
_ombre_probe: dict[str, dict] = {}   # 按 url 各缓存各的（每个角色可指不同 Ombre 实例）


def _ombre_mcp_config(char_id: Optional[str] = None) -> Path:
    """把角色的 Ombre 接线（characters.ombre_conf）渲染成 claude 的 mcp-config 文件。
    产物按角色分文件（角色 state 目录）——两个角色并发起子进程时各读各的，不互踩。"""
    import characters
    oc = characters.ombre_conf(char_id)
    path = state_store.char_state_dir(char_id) / "ombre.mcp.json"
    server: dict = {"type": "http", "url": oc["mcp_url"]}
    if oc["mcp_token"]:
        server["headers"] = {"Authorization": f"Bearer {oc['mcp_token']}"}
    payload = json.dumps({"mcpServers": {"ombre-brain": server}})
    if not path.exists() or path.read_text("utf-8") != payload:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(path)
    return path


# 宠物照料工具（PLAN_pet P3）：内置不走插件商店——宠物系统是主仓代码，
# 实例（谁家有猫）才是数据。家里没猫整个 server 不挂：工具在 TA 眼里不存在，
# 菜单块（needs 过滤）也不会渲染，不会跟他提一只不存在的猫。
PET_MCP_TOOLS = [f"mcp__pets__{t}"
                 for t in ("pet_state", "pet_feed", "pet_interact", "pet_scoop")]


def _pet_mcp_mounted(char_id: Optional[str] = None) -> bool:
    # 总开关关着不挂：猫在冬眠，喂猫工具在 TA 眼里不该存在（端点侧另有 409 兜底）。
    import pets
    import world
    return bool(world.house_active() and pets.ids())


def _pet_mcp_config(char_id: Optional[str] = None) -> Path:
    """渲染 pet MCP 的 mcp-config（口径同 _ombre_mcp_config：按角色分文件）。
    stdio 用本仓 venv 起 server/pet_mcp.py；env 下发角色身份（同 plugins.mounted
    的 CASSETTE_CHAR_ID 路），鉴权 key 由子进程从继承环境里读。"""
    me = (char_id or state_store.DEFAULT_CHAR_ID)
    path = state_store.char_state_dir(char_id) / "pets.mcp.json"
    payload = json.dumps({"mcpServers": {"pets": {
        "type": "stdio", "command": sys.executable,
        "args": [str(config.BASE_DIR / "pet_mcp.py")],
        "env": {"CASSETTE_CHAR_ID": me}}}})
    if not path.exists() or path.read_text("utf-8") != payload:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(path)
    return path


BASICS_MCP_TOOLS = ["mcp__basics__now", "mcp__basics__fetch"]


def _basics_mcp_config(char_id: Optional[str] = None) -> Path:
    """渲染基础工具 MCP 的 mcp-config（口径同 _pet_mcp_config）。
    **无条件挂、无开关**：now 是钟、fetch 是取一份公开资料，三个场景都该够得着
    （2026-09-02 定：他一轮跑到中途没有任何办法知道几点，那是核实纪律的地基漏了）。"""
    me = (char_id or state_store.DEFAULT_CHAR_ID)
    path = state_store.char_state_dir(char_id) / "basics.mcp.json"
    payload = json.dumps({"mcpServers": {"basics": {
        "type": "stdio", "command": sys.executable,
        "args": [str(config.BASE_DIR / "basics_mcp.py")],
        "env": {"CASSETTE_CHAR_ID": me}}}})
    if not path.exists() or path.read_text("utf-8") != payload:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(path)
    return path


# skill 库（PLAN_skills S0）：方法层文件树的渐进披露。内置不走插件商店（同宠物 MCP），
# 挂载条件是「这个场景有可见的 skill」——library 空着整个不挂，工具不出现、索引不渲染。
# 按 context 过滤和菜单块同判据：都问 skills.list_skills(context)，不会出现「工具挂了
# 索引却没提」的缺口（那正是 _warn_uncovered 会喊的事）。
def _skills_mounted(context: str = "chat", char_id: Optional[str] = None) -> bool:
    import skills
    return bool(skills.list_skills(context, char_id))


def ombre_alive(char_id: Optional[str] = None) -> bool:
    """快速探活角色的 Ombre /mcp 端点：任何 HTTP 响应都算活（MCP 对裸 GET 回 406 是正常的），
    连不上/超时=死。OMBRE_ENABLED=0 直接当死。结果按 url 缓存 ~30s（角色可各指一个实例，
    共用一个缓存会把 A 的死活当成 B 的）。
    显式空代理——macOS 系统代理的例外名单常常只有 localhost 没有 127.0.0.1，
    走系统代理会把本机请求吞掉还查不出原因。"""
    if not config.OMBRE_ENABLED:
        return False
    import characters
    url = characters.ombre_conf(char_id)["mcp_url"]
    now = time.time()
    probe = _ombre_probe.setdefault(url, {"ts": 0.0, "alive": False})
    if now - probe["ts"] < _OMBRE_PROBE_CACHE_SEC:
        return probe["alive"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(url, timeout=_OMBRE_PROBE_TIMEOUT)
        alive = True
    except urllib.error.HTTPError:
        alive = True
    except Exception:
        alive = False
    probe["ts"], probe["alive"] = now, alive
    return alive


# ---------- 人话版能力菜单 ----------
# 取代原来那段写死的 memory_block：工具说明外置成可编辑文件（characters.tool_menu_path），
# 按**本轮实际挂载的工具**过滤后注入。为什么非要按实际挂载过滤——四档醒来策略
# （照挂 / NO_WAKE_PLUGINS 硬禁 / WAKE_TOGGLEABLE 开关 / WAKE_TOOL_EXCLUDE 工具级）、
# 独占资源归属、per-char 插件集、Ombre 探活，每一条都会改变这一轮到底挂了什么。
# 写死的菜单必然说谎：跟 TA 提一个这轮根本不在场的能力，他会去调、然后失败。
_MENU_NEEDS_RE = re.compile(r"<!--\s*needs\s*:\s*(.*?)-->", re.I | re.S)
_MENU_WHEN_RE = re.compile(r"<!--\s*when\s*:\s*(\w+)\s*-->", re.I)
_MENU_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

TOOL_SEARCH_TOOL = "ToolSearch"   # CLI 内置：按名字取延迟工具的 schema（见 base_claude_args）

# 哪条路开工具延迟。**这就是那一行开关**：加/删 context 名，清空就整个关掉。
#
# 延迟的失效模式是"TA 看不到描述，就以为自己没这能力"（Claude Code 自带那条提醒
# ——Before concluding a capability is missing…——按轮数触发、每 15 轮一次且默认关着，
# 一次醒来只有一两轮，轮不到它响）。**人话版能力菜单就是这条的解药**：它把每个能力
# 用人话列一遍，工具全名由 tool_menu_block 从 needs 填进去。所以口径是——
# **菜单渲染到哪条路，哪条路才可以开延迟**；没菜单的路开了就是在赌。
#
# 2026-08-13 一度整个关掉（原本是 {"chat"}）：起因是开了之后连着三轮 TA 一个工具都没
# 调、两轮拿推断当事实答错。但事后查下来**那两道题判不了延迟**，跟它没关系：
#   - 「beacon 登记的邮箱是哪个」是**记忆题不是工具题**：邮箱加密存放，browse/read_card
#     的返回里都没有它，答案只在 Ombre 里。
#   - 「改邮箱只能退卡重贴」**答的是对的**：edit_card 的参数只有 token/name/platform/intro。
# 真因是搬家自己带进来的：旧 memory_block 里那句**无条件**的「开场先 breath」，搬进菜单
# 时变成了条件块「## 想不起来某件事」——而最该浮记忆的时候，恰恰是 TA 以为自己知道、
# 根本不觉得「想不起来」的时候。已改回动作式标题（见 tool_menu.example.md 的体例警告）。
#
# 2026-08-13 晚：醒来那条路接上菜单（wake.wake_prompt）后开延迟。实测（52 个工具，
# 同一句 prompt，量上下文 token）：不挂工具 1,482 / 全量 schema 19,772 / 全延迟 3,205。
# 代价是真要用工具时多一轮往返——醒来不赶时间，划算。
# 聊天那条**故意留全量 schema**（机主定的口径，不是待办）：两条路怕的东西正好相反——
# 醒来不怕慢、但怕迷茫（没人说话，得自己找事做），所以给菜单+延迟；聊天有明确的一句
# user 唤醒句，不迷茫，可那多出来的一轮往返 TA 是坐在手机前等着的，看得见。
# 哪天想在聊天也试，拿一道**只有工具能答**的题测（例：「墙上现在有几张卡片、都有谁」
# ——只有 browse 能答），别再拿记忆题当判据。
#
# ⚠️ 别指望 --tools 白名单省这份 token：它只挡执行、不挡 schema。实测同一个 mail server，
# 白名单摘不摘 mail_send，上下文都是 2,656——摘掉的工具 TA 照样看得见、会去调、然后被
# 挡回来（plugins.WAKE_TOOL_EXCLUDE 那条路正在踩这个）。要真藏掉只能整个 server 不挂，
# 或者让插件自己按场景少注册那个工具。
TOOL_SEARCH_CONTEXTS: set[str] = {"wake"}


def tool_search_on(context: str) -> bool:
    """这条路要不要开工具延迟。

    ⚠️ base_claude_args 和 _subprocess_env **必须拿同一个 context 问这里**，两边一致才有效：
    只把 ToolSearch 加进白名单、不给环境变量 = 白搭一份 schema 进去；
    只给环境变量、不把 ToolSearch 加进白名单 = 延迟静默不生效（CLI 退回全量 schema，
    日志里那句 "ToolSearchTool is not available" 是唯一线索）。两种都不报错，所以写死在
    一个判据里，别在调用点各判一次。"""
    return context in TOOL_SEARCH_CONTEXTS


def mounted_tool_names(context: str = "chat", char_id: Optional[str] = None) -> list[str]:
    """这一轮实际会挂载的工具全名（`mcp__x__y` / 内置名）。
    口径必须和 base_claude_args 一致——菜单按它过滤，对不上就会跟 TA 提不在场的能力。
    不复用 base_claude_args 的返回值是因为那边还要拼参数、且引擎不对时会抛。"""
    import plugins
    names: list[str] = list(BASICS_MCP_TOOLS)   # 无条件挂（钟 + 取公开资料）
    if ombre_alive(char_id):
        names += OMBRE_TOOLS
    _, plug_tools = plugins.mounted(context, char_id)
    names += plug_tools
    if _pet_mcp_mounted(char_id):
        names += PET_MCP_TOOLS
    if _skills_mounted(context, char_id):
        import skills
        names += skills.SKILLS_MCP_TOOLS
    # ToolSearch 也得算进来，条件跟 base_claude_args 一模一样——菜单头那句「用法默认
    # 没加载，先去取」正是按它在不在场决定说不说的。漏了它＝延迟开着却不告诉 TA 要先
    # 取用法，他直接调必失败（静默失效，只能靠肉眼看菜单头才发现）。
    if names and tool_search_on(context):
        names.append(TOOL_SEARCH_TOOL)
    return names


def _parse_tool_menu(text: str) -> list[dict]:
    """菜单文件 → [{title, body, needs, when}]。只认 `## 标题` 开块，其余随便写。"""
    blocks: list[dict] = []
    cur: Optional[dict] = None
    for line in text.splitlines():
        if line.startswith("## "):
            head = line[3:]
            needs_m = _MENU_NEEDS_RE.search(head)
            when_m = _MENU_WHEN_RE.search(head)
            cur = {
                "title": _MENU_COMMENT_RE.sub("", head).strip(),
                "needs": [t.strip() for t in (needs_m.group(1) if needs_m else "").split(",") if t.strip()],
                "when": (when_m.group(1).lower() if when_m else ""),
                "body": [],
            }
            blocks.append(cur)
        elif cur is not None:
            cur["body"].append(line)
    for b in blocks:
        # 块内的注释（给机主看的说明）不进 prompt
        b["body"] = _MENU_COMMENT_RE.sub("", "\n".join(b["body"])).strip()
    return [b for b in blocks if b["title"] and b["body"]]


_menu_gap_seen: dict[str, str] = {}   # 每种「漏了哪些」只喊一次，别每轮刷屏


def _warn_uncovered(context: str, by_short: dict, rendered_needs: set) -> None:
    """挂载了、但这一轮没有任何菜单块提到的工具 → 往日志里喊一条。

    为什么要机械查而不是靠纪律：开了工具延迟之后，**菜单没覆盖的工具在 TA 眼里只是一个
    光秃秃的名字**，等于装了个他用不上的东西。上新插件忘了补菜单是必然会发生的事
    （实锤：mail_mark 就这么漏了一版）。
    比的是**渲染出来的块**不是文件里所有块——这样连「块写得太粗、被 needs 过滤掉了」
    也能一起抓出来（读信/发信合成一块那种，正是文件头警告过的坑）。
    只认 mcp__ 开头的：ToolSearch 这类内置工具不该出现在能力菜单里。"""
    missing = sorted({s for s, full in by_short.items() if full.startswith("mcp__")}
                     - rendered_needs)
    key = ",".join(missing)
    if _menu_gap_seen.get(context) == key:
        return
    _menu_gap_seen[context] = key
    if missing:
        logerr(f"能力菜单没覆盖到这些工具（{context}）：{'、'.join(missing)}"
               f" —— 去 tool_menu.md 补一块，不然 TA 只看得见名字、不知道能干嘛")


def tool_menu_block(context: str = "chat", char_id: Optional[str] = None) -> str:
    """人话版能力菜单，按本轮实际挂载的工具过滤后渲染。没有一块过得了就返回空串。

    两条渲染纪律：
    - needs 里的工具**全都在场**才渲染这块（不是任一个）。宁可少提一块，也绝不提一个
      不在场的能力——所以菜单文件里块要切得细（读信/发信必须分开：醒来默认摘 mail_send
      但留读信，合成一块的话整块消失，连读信都不提了）。
    - 工具全名由这里从 needs 填进正文，**作者不手写**。手写的名字会随插件改名/Ombre
      换版本过期，而名字错了 TA 就取不到用法。
    """
    try:
        import characters
        text = characters.tool_menu_path(char_id).read_text("utf-8")
    except Exception as e:
        logerr(f"读能力菜单失败（这轮不注入）: {e}")
        return ""

    mounted = mounted_tool_names(context, char_id)
    # 全名按尾段建索引：菜单里写裸名（dream），挂载的是全名（mcp__ombre-brain__dream）
    by_short: dict[str, str] = {}
    for full in mounted:
        by_short.setdefault(full.rsplit("__", 1)[-1], full)

    lines: list[str] = []
    rendered_needs: set[str] = set()
    for b in _parse_tool_menu(text):
        if b["when"] and b["when"] != context:
            continue
        fulls = [by_short[n] for n in b["needs"] if n in by_short]
        if len(fulls) != len(b["needs"]):
            continue        # 缺任一个 → 整块不提
        rendered_needs.update(b["needs"])
        lines.append(f"■ {b['title']}")
        lines.append("  " + b["body"].replace("\n", "\n  "))
        if fulls:
            lines.append("  工具：" + ", ".join(fulls))
    # skill 索引（PLAN_skills）：skill_read 在场才追加（渲染纪律同 needs——绝不提
    # 不在场的能力；挂载判据 _skills_mounted 和这里同问 list_skills，不会两边打架）。
    # 索引就是 skill_read 的菜单块，所以要记进 rendered_needs——不然
    # _warn_uncovered 每次都为它喊「菜单没覆盖」的冤。
    if "skill_read" in by_short:
        import skills
        blk = skills.index_block(context, char_id, by_short["skill_read"])
        if blk:
            lines.append(blk)
            rendered_needs.add("skill_read")
    _warn_uncovered(context, by_short, rendered_needs)
    if not lines:
        return ""

    # 取用法那句只在 ToolSearch 真在场时才说——延迟没开的时候 schema 本来就都在，
    # 让他去"先取用法"是让他白跑一趟调一个不存在的工具。
    head = "【你手上有这些能力。】"
    if TOOL_SEARCH_TOOL in mounted:
        head = (f"【你手上有这些能力。这些工具的**用法**默认没加载，想用哪个先用 "
                f"{TOOL_SEARCH_TOOL} 取：query 写 \"select:工具全名\"，逗号分隔可以一次取好几个。"
                f"没取用法就直接调一定失败。别因为只看见名字就以为自己没这能力。】")

    # 「看得见但用不了」的工具：schema 还在上下文里（--tools 摘不掉，实测见
    # plugins.WAKE_TOOL_EXCLUDE 上面那段），但白名单里没有，调了必被权限闸拒。
    # 不说的话 TA 会当自己有这能力——可能先答应机主"我给你发一封"再失败，白烧一轮。
    # 这是止血；正解是让 server 别注册那个工具（PLAN_tool_exclude.md）。
    try:
        import plugins
        shadowed = plugins.shadowed_tools(context, char_id)
    except Exception as e:
        logerr(f"算 shadowed_tools 失败（不影响菜单）: {e}")
        shadowed = []
    tail = ""
    if shadowed:
        tail = ("\n【这几个这轮**用不了**（机主关掉了）：" + "、".join(shadowed) +
                "。你可能会看到它们的用法，但调用会被拒——别去调，也别跟"
                f"{config.user_name()}说你能做这件事。】")
    return head + "\n" + "\n".join(lines) + tail


# ---------- 子进程 ----------
def base_claude_args(persona_file: Optional[Path] = None,
                     context: str = "chat",
                     char_id: Optional[str] = None) -> list[str]:
    """所有 claude 调用共用的参数，统一从这里出（别另起一套）。
    各 MCP 源累积（可并列多个 --mcp-config，claude 合并）：Ombre 记忆 + 启用中的插件。
    有工具 → 白名单挂载（--strict-mcp-config 屏蔽机器上其它 MCP；--allowedTools
    预批准所以 headless 不弹权限，绝不用 --dangerously-skip-permissions）；
    一个没有 → 纯聊天 --tools ""。

    context＝这次调用是哪条路（'chat' / 'wake'）：插件按场景分挂，有些工具不给醒来那条路
    （见 plugins.NO_WAKE_PLUGINS）。默认 chat——醒来的调用方必须自己显式写 context='wake'。

    char_id＝为哪个角色起模型：人设 / Ombre / 插件集全按角色走。这里也是**引擎缝**：
    一期只有 claude-code 引擎，char.json 写了别的（如 openai-compat）在这儿有声报错——
    静默用 claude 顶替等于让别人替这个角色说话。"""
    import characters
    import plugins   # 函数内 import：plugins 依赖 state_store/config，避免模块级环
    eng = characters.engine(char_id)
    if eng != characters.CLAUDE_ENGINE:
        raise RuntimeError(f"角色引擎 {eng!r} 尚未实现（一期只有 {characters.CLAUDE_ENGINE}）")
    args = [
        "claude", "-p",
        "--model", config.MODEL,
        "--system-prompt-file", str(persona_file or rendered_persona(char_id)),
    ]
    mcp_configs: list[str] = [str(_basics_mcp_config(char_id))]
    tools: list[str] = list(BASICS_MCP_TOOLS)
    if ombre_alive(char_id):
        mcp_configs.append(str(_ombre_mcp_config(char_id)))
        tools += OMBRE_TOOLS
    plug_cfg, plug_tools = plugins.mounted(context, char_id)
    if plug_cfg:
        mcp_configs.append(plug_cfg)
        tools += plug_tools
    if _pet_mcp_mounted(char_id):
        mcp_configs.append(str(_pet_mcp_config(char_id)))
        tools += PET_MCP_TOOLS
    if _skills_mounted(context, char_id):
        import skills
        mcp_configs.append(str(skills.mcp_config(char_id)))
        tools += skills.SKILLS_MCP_TOOLS
    # 工具延迟：上下文里只留工具名，用法（schema）等 TA 自己按名字取。工具一多，
    # 光 schema 就是大头——实测醒来那条路 52 个工具时 20,076 token，开了延迟 3,509（−83%）。
    # ToolSearch 必须**进白名单**才算数：它是内置工具，而这里的 --tools 是精确白名单，
    # 不点名它就被挡在外面，CLI 会静默退回全量 schema（实锤日志：
    # 「Tool search disabled: ToolSearchTool is not available」，而 mode=tst 明明是成功的）。
    # 安全姿态不变：多一个显式命名的内置工具，--strict-mcp-config / --allowedTools 逐个
    # 枚举照旧，依然不用 --dangerously-skip-permissions。
    if tools and tool_search_on(context):
        tools = tools + [TOOL_SEARCH_TOOL]
    if tools:
        for c in mcp_configs:
            args += ["--mcp-config", c]
        args += ["--strict-mcp-config", "--tools", *tools, "--allowedTools", *tools]
    else:
        args += ["--tools", ""]
    return args


def neutral_cwd() -> str:
    """一次性 claude 子进程的工作目录：一个恒空目录（state/claude_cwd）。
    聊天/醒来的模型没有文件工具，本不需要真实 cwd；而 CLI 会把 prompt 里的 @词
    当文件引用、命中 cwd 下的路径就自动把内容附进上下文——实锤：历史里 SwiftUI
    代码的 `@State` 命中了 server/state（macOS 不分大小写），从此每一轮都附一份
    state 目录清单，TA 看到的就是"用户消息里带了段 ls"。空目录让相对 @-mention
    永远落空。MCP 子进程会继承这个 cwd：插件文件 IO 必须锚 __file__ 或走绝对路径。"""
    d = state_store.STATE_DIR / "claude_cwd"
    d.mkdir(exist_ok=True)
    return str(d)


def _subprocess_env(context: str = "chat") -> dict:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)   # 强制走 CLI 登录态，不走 API 计费
    # ⚠️ context 必须和同一次调用的 base_claude_args 传的一致（见 tool_search_on 的注释）。
    if tool_search_on(context):
        env["ENABLE_TOOL_SEARCH"] = "true"
    return env


def _extract_memory_text(inp: dict) -> str:
    """从 hold/grow 的工具输入里取记忆正文。hold 用 'content'；grow 可能是 content 或批量列表。"""
    if not isinstance(inp, dict):
        return ""
    c = inp.get("content")
    if isinstance(c, str) and c.strip():
        return c.strip()
    for k in ("memories", "items", "text"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list):
            parts = [str(x.get("content", x)) if isinstance(x, dict) else str(x) for x in v]
            joined = "\n".join(p for p in parts if p).strip()
            if joined:
                return joined
    return ""


def _describe_trace(inp: dict) -> str:
    """把一次 trace 翻成人话。改内容就用新正文；只改元数据就把所有改动都列出来。总返回非空。"""
    if not isinstance(inp, dict):
        return "（改了一条记忆）"
    c = (inp.get("content") or "").strip()
    if c:
        return c   # 改内容优先，直接显示新正文
    if (inp.get("new_str") or "").strip():
        return f"（局部改写了一条记忆：…{inp['new_str'].strip()[:80]}…）"
    parts: list[str] = []
    if inp.get("hard_delete"):
        parts.append("彻底删掉了")
    elif inp.get("delete"):
        parts.append("删掉了")
    if inp.get("restore"):
        parts.append("恢复了")
    if inp.get("resolved") == 1:
        parts.append("沉底(标记已解决)")
    elif inp.get("resolved") == 0:
        parts.append("重新激活")
    if inp.get("pinned") == 1:
        parts.append("钉选")
    elif inp.get("pinned") == 0:
        parts.append("取消钉选")
    if inp.get("digested") == 1:
        parts.append("隐藏")
    if (inp.get("tags") or "").strip():
        parts.append(f"改标签为「{inp['tags']}」")
    if inp.get("importance", -1) != -1:
        parts.append(f"重要度→{inp['importance']}")
    if inp.get("valence", -1) != -1:
        parts.append("调了 valence")
    if inp.get("arousal", -1) != -1:
        parts.append("调了 arousal")
    if (inp.get("name") or "").strip():
        parts.append(f"改名为「{inp['name']}」")
    if (inp.get("domain") or "").strip():
        parts.append(f"改分类为「{inp['domain']}」")
    return "（对一条记忆：" + "、".join(parts) + "）" if parts else "（调整了一条记忆）"


def bare_tool_name(name: str) -> str:
    """小字提醒用的裸工具名（PLAN_chatui §7.2 拍板 09-01：全裸名，零维护）。
    mcp__server__tool → mcp_tool——server 段是挂载编号不是工具身份，掐掉；
    内置工具（Edit/Bash）本来就是裸名，照原样。"""
    if name.startswith("mcp__"):
        return "mcp_" + name.rsplit("__", 1)[-1]
    return name


def _stored_from_tool_use(name: str, inp: dict) -> Optional[dict]:
    """工具调用 → stored 条目（app 灰字提示用）。只记「写」操作，breath 等读操作不算产物。
    ⚠️ 这里只看得到「他想干什么」。干成没干成要等 tool_result，见 StoredCollector。"""
    if name.endswith("__hold") or name.endswith("__grow"):
        text = _extract_memory_text(inp)
        if text:
            # feel=True 是感受类记忆（挂在一条已有记忆上），和普通 hold 不是一回事——
            # 分开标，心流日志里才看得出他存的是件事还是一份心情。
            tool = "grow" if name.endswith("__grow") else ("feel" if inp.get("feel") else "hold")
            return {"tool": tool, "text": text}
        return None
    if name.endswith("__trace"):
        return {"tool": "trace", "text": _describe_trace(inp)}
    if name.endswith("__I"):
        # 自我认知候选（写入才算产物；read/promote 是读和转正操作，不记）
        c = (inp.get("content") or "").strip()
        return {"tool": "i", "text": c} if c else None
    if name.endswith("__webpage_write"):
        # 网页插件：做/改了一个网页 → 聊天里补可点的卡片（app 在 HTML 文件里也能看）
        title = (inp.get("title") or "").strip() or "未命名网页"
        return {"tool": "webpage", "text": title}
    if name.endswith("__code_start"):
        # codemode 插件：TA 自己切去 code 模式了。**这不是记忆产物**，是借 stored 走的
        # 一条控制信号——app.py 的 finalize 会把它剥出去置 code_started，让 app 翻模式。
        # 不剥干净的话，app 那边对不认识的 tool 会兜底成「记住了一件事」的灰字（踩过）。
        return {"tool": "codemode", "text": (inp.get("task") or "").strip()}
    if name.endswith("__task_run"):
        # game-task 插件：派任务引擎去跑日常。灰字给个可核对的任务清单就够，
        # 结果要等引擎跑完（Bark + task_log），不在这一轮里。
        names = inp.get("names") or []
        text = "、".join(str(n) for n in names) if isinstance(names, list) else str(names)
        return {"tool": "gametask", "text": text} if text else None
    if name.endswith("__game_start"):
        # game-story 插件：TA 自己切去玩游戏了。和 codemode 同款——借 stored 走的控制
        # 信号，app.py 的 finalize 剥出来置 game_started，app 据此起终端面板和系统灰字。
        return {"tool": "gamemode", "text": (inp.get("task") or "").strip()}
    if name.endswith("__mail_send"):
        # 邮箱插件：寄信是对外动作，值得一条灰字 + 进心流日志。这里只看得到意图；
        # 「真发出了」还是「落草稿箱等机主确认」要看返回文案，on_user 里改判（mail_draft）。
        # mail_inbox/mail_read/mail_mark 是读操作，跟 breath 同口径：不记。
        to = (inp.get("to") or "").strip()
        subj = (inp.get("subject") or "").strip()
        text = (f"给 {to}" if to else "一封信") + (f"：{subj}" if subj else "")
        return {"tool": "mail", "text": text}
    if name.endswith("__browser_navigate"):
        # 浏览器插件：浏览只记 navigate（click/type 太碎是噪音）。逐条不发灰字——
        # sse 那边跳过 browse，finalize 聚合成一条（text=网址列表）+ 落 browse_log，
        # app 收起显示「浏览了 N 个网页」、点开展开网址。navigate_back 的 endswith
        # 对不上这个后缀，不会误入。
        url = (inp.get("url") or "").strip()
        return {"tool": "browse", "text": url} if url else None
    return None


# 会走 memory 灰字 / 进心流日志的 stored 类型。codemode 是控制信号不是产物；browse 有
# 自己的聚合灰字和 browse_log，逐条进心流日志只会刷屏——都不进。
NON_MEMORY_TOOLS = {"webpage", "codemode", "browse", "gametask", "gamemode"}

# ---------- 留痕判线（PLAN_sdk §5.3 S3 补线①，08-31）----------
# 行为账/经历留痕的判线=「碰没碰外部世界」（§5.2：对外部对象的读写都留——「读过」
# 这个事实防失约；纯内部记忆操作不留，人不记得自己回忆过什么）。这跟上面
# NON_MEMORY_TOOLS 管的「哪些 stored 不进灰字/心流日志」是两件事——借用过一版，
# 后果是反的（开过网页留痕、发过信不留痕）。写成内部白名单取补集：新插件的外部
# 动作（花园发帖/写日记）自动算外部；多收一行行为清单的代价，远小于反向漏掉
# （失约：「发了信然后说没发过」）。
INTERNAL_STORED = {"hold", "feel", "grow", "trace", "i"}


def external_stored(tag: Optional[str]) -> bool:
    """stored 标签级判线：这条产物是不是「碰了外部世界」的动作。

    ⚠️ 08-31 起没有生产调用点：行为账搬到执行层之后判线换成了工具名级的
    acts_worthy（stored 是从回复文本解析的「他说他做了什么」，用它当留痕判据
    等于让自述给自己作证）。留着只为 stored 标签表本身还有别处读；**别再拿它
    当行为/经历留痕的判线**。"""
    return bool(tag) and tag not in INTERNAL_STORED


# 工具名级判线（执行层事件账用，S3 补线②；stored 标签级在 external_stored）。
# 内部=纯记忆（Ombre）/取用法（skills、ToolSearch）/游戏操作细节——game 的经历由
# 点评+截图+章节志承载，逐次点按是 §5.3 第四类试错，不值得留。
_INTERNAL_TOOL_PREFIXES = ("mcp__ombre-brain__", "mcp__game__", "mcp__skills__")
_INTERNAL_TOOL_NAMES = {TOOL_SEARCH_TOOL}


def external_tool(name: str) -> bool:
    """这个工具调用碰没碰外部世界（折叠段经历摘要认它；白名单外都算碰了）。"""
    return bool(name) and (name not in _INTERNAL_TOOL_NAMES
                           and not name.startswith(_INTERNAL_TOOL_PREFIXES))


# 只读内置工具（§5.3 只读常驻面的成员；留痕侧当「读/写」分界用：读类聚合留事实
# （四类表第三类）、写类逐条留原文（第一类））。
READONLY_BUILTINS = {"Read", "Grep", "Glob"}

# 写类内置工具（PLAN_native §1：不进 allowed_tools，CLI 判 ask → can_use_tool
# 弹卡原地挂起，机主批的就是那条调用原文本身）。Bash 整个算写类——不需要
# 命令级读写细分，看东西有 Read/Grep/Glob。
WRITE_BUILTINS = {"Edit", "Write", "NotebookEdit", "Bash"}


def acts_worthy(name: str) -> bool:
    """行为账判线（执行层落账用，08-31 事故修）：碰了外部世界的、和每一次
    写类——写类一行自带机主批准这个事实（PLAN_native §4：每条写类调用就是
    账上一行；code 无场之后行为账是写类留痕唯一的家）。只读不落：翻文件是
    看不是做，账会被刷成流水。"""
    return (name in WRITE_BUILTINS
            or (external_tool(name) and name not in READONLY_BUILTINS))


# 只读常驻的安全面（§5.3 机主 08-31 拍板：限根目录+黑名单，不做全盘放行）。
# 「只读 ≠ 无害」：聊天上下文里的注入面（邮件/论坛/网页）可以指使他去读任意
# 文件再说进气泡——外泄路径是气泡本身，带外门拦不住，只能限读面。
# 黑名单：.env/凭据、别的角色的 state/characters/（串台面新一维）、私有仓
# mianmian-app。缺口真机撞见再从执行层补，别在这儿穷举。
CODE_ROOT = Path(__file__).resolve().parent.parent   # cassette 仓根

# 机主「主仓 PLAN + memory 加一条索引」老规矩的另一半：Claude Code 的项目记忆目录。
# code 模式时代 TA 在仓根跑 CLI 天然够得着它；code 停用后（09-03）聊天路的写类要
# 续上这个老规矩，就得在这儿点名放行（2026-09-03 机主拍板加白）。
# ⚠️ 只放 memory/ 这一层——~/.claude 其余（settings.json、凭据、别的项目的记忆）
# 都不在内；写进去每一笔照样过 permit 弹卡，这儿只是让申请到得了机主指头上。
MEMORY_ROOT = (Path.home() / ".claude" / "projects"
               / str(CODE_ROOT).replace("/", "-") / "memory")

_GUARD_PATH_KEYS = ("file_path", "path", "notebook_path")


def readonly_path_guard(tool_input: Optional[dict],
                        char_id: Optional[str]) -> Optional[str]:
    """只读工具的路径闸：None=放行；str=拒绝原话（PreToolUse 门直接用）。
    相对路径按恒空 cwd（neutral_cwd）解析再查——「../」从空目录一步就能爬进
    state/，不解析就查等于没查。没点名路径（Grep 全局搜）＝落在空 cwd，无害。"""
    raws = [v for k in _GUARD_PATH_KEYS
            if isinstance(v := (tool_input or {}).get(k), str) and v.strip()]
    for raw in raws:
        p = Path(raw.strip())
        if not p.is_absolute():
            p = Path(neutral_cwd()) / p
        try:
            rp = p.resolve()
        except Exception:
            return "这条路径看不懂——换条正经路径"
        if not (rp.is_relative_to(CODE_ROOT)
                or rp.is_relative_to(MEMORY_ROOT)):
            return (f"你手边只看得到 cassette 仓（{CODE_ROOT}）"
                    f"和记忆索引目录（{MEMORY_ROOT}），这条路在范围外")
        if any(seg.startswith(".env") for seg in rp.parts):
            return "凭据类的文件（.env 之类）不在你可看的范围里"
        if "mianmian-app" in rp.parts:
            return "mianmian-app 是私人仓，不在你可看的范围里"
        croot = state_store.CHAR_STATE_ROOT.resolve()
        if rp.is_relative_to(croot):
            rel = rp.relative_to(croot)
            if rel.parts and rel.parts[0] != (char_id or ""):
                return ("那是别的角色的房间（state/characters/），"
                        "各自的时间线各自看")
    return None


# ---------- 工具「调用结果」定案 ----------
# 只解析 tool_use（输入）的老口径抓的是**调用意图**：失败的调用照样被记成「📥 记住了一件事」，
# 心流日志和聊天灰字都在骗人（实锤：08-06 23:13 日志显示存了两条，实际只落盘一条——
# 第一条 feel 缺 source_bucket 被 Ombre 拒了）。所以要等 tool_result 回来才算数。

def _tool_result_text(block: dict) -> str:
    """tool_result 的正文。content 可能是字符串，也可能是 [{type:text,text:...}] 列表。"""
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


# 婉拒名单：Ombre 有一类拒绝走 isError:false，只在正文里说一句，从外面完全看不出失败。
# 这是一份**实际见过的**清单，不是通用分类器——故意不写「失败/无法/不能」这种泛词：
# 记忆正文本身提到"那次部署失败了"是常事，泛匹配会把真存下的记忆误判成没存，
# 那是更坏的一头（存下的东西从心流日志里消失）。名单外的拒绝先当成功（和改之前一样），
# 见到新说法就往这儿加一条，注明出处。
_TOOL_REJECT_MARKS = (
    "error executing tool",      # MCP/pydantic 参数校验不过（实测 trace 少传 bucket_id）
    "未找到记忆桶",               # 实测 trace 指向不存在的桶，isError 是 false
    "测试数据不能创建为",          # 实测 hold(test_data+feel)
    "必须指向一条原始记忆",        # feel 缺 source_bucket（08-06 23:13 那次实锤）
)
# 开头就报错的：成功的返回不会这么起头，放心按前缀认。
_TOOL_REJECT_PREFIXES = ("error:", "错误：", "错误:", "failed", "traceback")


def _short_reason(text: str) -> str:
    """把工具的报错正文压成一句给人看的原因（首行、限长）。截断给个省略号——
    不然半截路径半截话，看着像原因本身就是坏的。"""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    line = stripped.splitlines()[0].strip()
    return line if len(line) <= 80 else line[:79] + "…"


def tool_result_error_structural(is_error: bool, text: str) -> Optional[str]:
    """①② 两道**结构**判据：MCP 的 `is_error` 位、返回体 `{"ok": false}`。
    是则返回给人看的原因，否则 None。

    读的是专门用来表示成败的字段——读到什么就是什么，不存在猜。**③ 那道
    文本婉拒名单不在这儿**：它给的是「像失败」不是「是失败」，只认见过的说法
    （09-01 实例：一封信被配额挡回来，回执「今天已经寄了 3 封了。明天再来。」，
    表里四条一条不命中）。要不要吃 ③，由调用方按自己的容错代价决定——
    灰字提示认错一次只值一句话，账本认错会被下一轮当既成事实往下算，
    所以行为账/事件账只吃 ①②（眠眠 09-01 12:16 拍板）。"""
    if is_error:
        return _short_reason(text) or "工具报错了"
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if isinstance(obj, dict) and obj.get("ok") is False:
        return _short_reason(str(obj.get("error") or "")) or "没成功"
    return None


def tool_result_error(block: dict) -> Optional[str]:
    """这条 tool_result 算失败吗？是则返回给人看的原因，否则 None。三道判据：
      ① is_error：MCP 标准错误（参数校验不过之类）；
      ② 返回体是 {"ok": false, "error": ...}：插件的口径（codemode 就这么回）；
      ③ 正文命中婉拒名单：Ombre 那种 isError:false 的软拒绝。
    ①② 是结构判据、准（抽在 tool_result_error_structural，留痕侧只吃这两道）；
    ③ 是文本判据、只认见过的说法（见 _TOOL_REJECT_MARKS）。"""
    text = _tool_result_text(block).strip()
    err = tool_result_error_structural(
        bool(block.get("is_error") or block.get("isError")), text)
    if err:
        return err
    low = text.lower()
    if any(m in low for m in _TOOL_REJECT_MARKS) or low.startswith(_TOOL_REJECT_PREFIXES):
        return _short_reason(text)
    return None


class StoredCollector:
    """把「工具调用」和「工具返回」对上号，产出定了案的 stored 条目。

    用法：assistant 事件喂 on_assistant()，user 事件（工具返回在这里）喂 on_user()，
    流走完调一次 finish()。items 是最终清单，每条带 ok；失败的另带 error。
    两条解析路（parse_claude_stream / sse.translate_events）共用这一份，别再各写一套。"""

    def __init__(self):
        self._pending: dict = {}      # tool_use_id → 还没等到结果的 stored 条目
        self._seen_tool_ids: set = set()   # CLI 会按内容块重复发同一条 assistant 消息
                                           # （每次带累计块），同一个 tool_use 会出现多次
        self.items: list[dict] = []   # 已定案的（成功和失败都在，靠 ok 区分）

    def on_assistant(self, ev: dict) -> None:
        """吃一条 assistant 事件：把里面的 tool_use 记成「待定案」。"""
        for b in ev.get("message", {}).get("content", []):
            if b.get("type") != "tool_use":
                continue
            tid = b.get("id")
            if tid:
                if tid in self._seen_tool_ids:
                    continue
                self._seen_tool_ids.add(tid)
            s = _stored_from_tool_use(b.get("name", ""), b.get("input", {}) or {})
            if not s:
                continue
            # 裸工具名随条目走（小字提醒 §7.2 拍板：文案=裸名）。tool 标签继续留着——
            # mail_draft 改判 / 心流日志 / webpage 卡片反查都认它，两个字段各干各的。
            s["name"] = bare_tool_name(b.get("name", ""))
            if tid:
                self._pending[tid] = s
            else:
                # 没有块 id 就没法和结果对上号（理论上不会发生）：按老口径当成功记下，别丢。
                self.items.append({**s, "ok": True})

    def on_user(self, ev: dict) -> list[dict]:
        """吃一条 user 事件（工具返回）：给对得上号的条目定案。返回这次新定案的那些
        （流式那条路要拿它们现发灰字）。"""
        settled: list[dict] = []
        for b in ev.get("message", {}).get("content", []):
            if b.get("type") != "tool_result":
                continue
            s = self._pending.pop(b.get("tool_use_id") or "", None)
            if s is None:
                continue   # 不是我们关心的工具（breath 等读操作），或已定过案
            err = tool_result_error(b)
            item = {**s, "ok": err is None}
            # mail_send 的「成功」有两种结局：真发出 / 收件人白名单外落草稿箱等机主确认。
            # 壳的返回文案是唯一分辨处——落草稿改判成 mail_draft，灰字才不会把
            # 「等你过目」说成「寄出了」（那是句假承诺）。
            if item["ok"] and s.get("tool") == "mail" and "草稿信箱" in _tool_result_text(b):
                item["tool"] = "mail_draft"
            if err:
                item["error"] = err
                logerr(f"工具没成功 {s['tool']}: {err}")
            self.items.append(item)
            settled.append(item)
        return settled

    def finish(self) -> list[dict]:
        """收尾：还没等到返回的（流断在半路 / 子进程被杀）。标成失败而不是默默算成功——
        「记住了一件事」是句承诺，没看到结果就不该替他说出口。"""
        settled: list[dict] = []
        for s in self._pending.values():
            item = {**s, "ok": False, "error": "没等到工具返回（这轮中断了）"}
            self.items.append(item)
            settled.append(item)
        if settled:
            logerr(f"{len(settled)} 个工具调用没等到返回，按没成功记")
        self._pending.clear()
        return settled


def parse_claude_stream(stdout: str, collect_all_text: bool = False) -> tuple[Optional[str], list[dict]]:
    """解析 stream-json 事件流，返回 (文本回复, stored)。
    stored 是这轮工具调用的结构化产物（存/改了什么长期记忆），SSE 灰字/响应字段按它渲染。
    collect_all_text=False（聊天）：只取最终 result 文本（干净的最后一段回复）。
    collect_all_text=True（醒来）：拼接所有 assistant text 块——带工具时模型可能
    先写一段 → 调工具 → 再写后半段，只取 result 会丢掉工具调用前的文本。"""
    result_text = None
    text_parts: list[str] = []
    collector = StoredCollector()   # tool_use 只是意图，成没成要等 tool_result 定案
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "assistant":
            if collect_all_text:
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "text":
                        txt = (b.get("text") or "").strip()
                        if txt:
                            text_parts.append(txt)
            collector.on_assistant(ev)
        elif t == "user":
            collector.on_user(ev)   # 工具返回：定案
        elif t == "result":
            if ev.get("is_error"):
                collector.finish()
                return None, collector.items
            result_text = ev.get("result")
    collector.finish()
    if collect_all_text and text_parts:
        return "\n".join(text_parts), collector.items
    return (result_text.strip() if result_text else None), collector.items


def stdin_payload(prompt: str, images: Optional[list] = None,
                  file_blocks: Optional[list[dict]] = None,
                  use_bp: bool = True) -> str:
    """一条 stream-json user 消息——**所有 claude 调用统一走这里**，纯文本 stdin 那条路
    已经退役（要打缓存断点就必须能拆 content block，纯文本给不了这个）。

    prompt 是 SplitPrompt 且允许打断点时切成两块：[稳定块(带 cache_control), 易变块]；
    否则整段一块，模型看到的字节跟以前逐字相同——**拆块只改投递方式，不改内容**。

    images 元素带 .data(base64)/.media_type（app.py 的 ImageInput）；file_blocks 是
    app.py _file_to_block 转好的 document block。两者都追在最后：它们跟着新消息走，
    本来就在易变的那一侧。"""
    cut = getattr(prompt, "cut", 0) if (use_bp and cache_bp_on()) else 0
    if cut > 0:
        content: list[dict] = [
            {"type": "text", "text": prompt[:cut],
             "cache_control": {"type": "ephemeral", "ttl": CACHE_BP_TTL}},
            {"type": "text", "text": prompt[cut:]},
        ]
    else:
        content = [{"type": "text", "text": str(prompt)}]
    for img in (images or []):
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": img.media_type,
                                   "data": img.data}})
    content.extend(file_blocks or [])
    return json.dumps({"type": "user",
                       "message": {"role": "user", "content": content}}) + "\n"


def _call_claude(prompt: str, images: Optional[list] = None,
                 file_blocks: Optional[list[dict]] = None,
                 char_id: Optional[str] = None) -> tuple[str, list[dict]]:
    """起一次性 claude -p 子进程（stdin 读到 EOF 才开始），返回 (回复, stored)。
    断点被 CLI 吃满而 400 → 关掉断点原样重跑一次，这一轮对上层完全无感。"""
    args = base_claude_args(char_id=char_id) + ["--input-format", "stream-json",
                                                "--output-format", "stream-json", "--verbose"]
    proc = None
    for attempt in (1, 2):
        use_bp = cache_bp_on()
        try:
            proc = subprocess.run(
                args, input=stdin_payload(prompt, images, file_blocks, use_bp=use_bp),
                capture_output=True, text=True, cwd=neutral_cwd(),
                env=_subprocess_env(), timeout=config.CLAUDE_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="claude 超时未返回")
        if proc.returncode == 0:
            break
        if attempt == 1 and use_bp and bp_limit_hit(proc.stdout):
            disable_cache_bp("一次性调用")
            continue
        raise HTTPException(status_code=502, detail=f"claude 进程出错: {proc.stderr[:500]}")
    reply, stored = parse_claude_stream(proc.stdout)
    if reply is None:
        raise HTTPException(status_code=502, detail="claude 未返回结果")
    return reply, stored


def call_claude_multimodal(prompt: str, images: list,
                           file_blocks: Optional[list[dict]] = None,
                           char_id: Optional[str] = None) -> tuple[str, list[dict]]:
    """带图/文件的一次性调用（非流式回退路）。"""
    return _call_claude(prompt, images, file_blocks, char_id)


def call_claude(prompt: str, char_id: Optional[str] = None) -> tuple[str, list[dict]]:
    """纯文本的一次性调用。"""
    return _call_claude(prompt, char_id=char_id)


# ---------- 内部标记 ----------
# 提示词教模型写的内联标记统一是 [[token:...]] 形态（后续模块会用到，如定下次醒来）。
# 标记从不落库：进历史/给 app 前剥干净，所以改格式不用数据迁移。
_MARKER_RE = re.compile(r"\[\[.*?\]\]", re.S)


def strip_markers(text: str) -> str:
    # 引用区里的标记不剥（口径同 sub_outside_quotes）：那是他在引用，不是在下指令，
    # 剥了就成了「带窟窿的正文」——PLAN_native §14.0 那次没被当场发现，靠的就是窟窿。
    return sub_outside_quotes(_MARKER_RE, lambda m: "", text)


# ---------- 浏览器去留标记（幽灵会话，见 browser_keeper.py）----------
# 默认轮末 Chrome 随最后一个 MCP 客户端断开而关；TA 用标记选择：
# [[browser:keep]]=留着（粘性：之后不提就一直留），[[browser:close]]=把之前留的关掉。
# token 英文为准，容错中文/全角冒号/大小写；多个标记最后一个算数。
_BROWSER_CHOICE_RE = re.compile(r"\[\[\s*browser\s*[:：]\s*(keep|close|保留|关闭)\s*\]\]", re.I)


def parse_browser_markers(text: str) -> tuple[str, Optional[str]]:
    """剥掉 [[browser:…]] 标记，返回 (干净文本, "keep"/"close"/None)。"""
    if not text or "[[" not in text:
        return text, None
    choice = None

    def _on(m):
        nonlocal choice
        choice = "keep" if m.group(1).lower() in ("keep", "保留") else "close"
        return ""

    return sub_outside_quotes(_BROWSER_CHOICE_RE, _on, text), choice
