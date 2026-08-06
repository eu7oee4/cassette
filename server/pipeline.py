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
def build_context_timeline(conv_items: list[dict], reflect_limit: int = 5) -> str:
    """把 最近对话 + 模型自己醒来时的内心（用户没看到）合并成一条按时间排序的时间线。
    醒来和聊天共用——解决「对话本身没时间戳、和内心对不上先后顺序」的问题。"""
    items: list[tuple[int, str]] = []

    for c in conv_items:
        ts = c.get("ts")
        if ts is None:
            continue
        who = config.user_name() if c.get("role") == "user" else "你"
        items.append((int(ts), f"{who}：{c.get('text', '')}"))

    # 最近几次醒来的内心（含 none；用户看不到）。只读日志尾部——append-only 文件会一直长。
    for w in [e for e in state_store.read_wake_log(limit=100) if (e.get("thoughts") or "").strip()][-reflect_limit:]:
        act = {"none": "没做什么", "message": "发了消息"}.get(w.get("action"), "")
        # 被打扰控制拦下的消息：标清楚没送出去，别让他以为发过了接着那条往下聊。
        if w.get("action") == "message" and not w.get("pushed"):
            act = "想发消息但被打扰控制拦下、没送出去"
        th = (w.get("thoughts") or "").strip().replace("\n", " ")
        if len(th) > 140:
            th = th[:140] + "…"
        # 「内心/你想」的措辞本身就表达了"没说出口"，不用额外标注可见性。
        items.append((int(w.get("ts", 0)), f"〔你醒来·{act}〕你想：{th}"))

    items.sort(key=lambda x: x[0])
    return "\n".join(f"[{fmt_ts(ts)}] {txt}" for ts, txt in items)


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
        lines.append("如果你觉得某张的描述不准，可以顺手改：[[sticker_desc:s1=新的描述]]。")
    return "\n".join(lines)


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

    reply = _STICKER_DESC_RE.sub(on_desc, reply)
    reply = _STICKER_SEND_RE.sub(on_send, reply)
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


def _chat_next_hint() -> str:
    # 函数不是模块级常量：名字用户随时可改，import 时冻结就换不动了。
    return (f"【可选：如果{config.user_name()}提到要离开/回来/睡觉之类，你可以顺手安排下次主动醒来——"
            "在回复里写 [[next_wake:3小时]]（范围 5 分钟~12 小时，会被剥掉、对方看不到）。没必要就别写。】")


def parse_next_minutes(section: str) -> Optional[int]:
    """把「90分钟」「3小时」这类字样解析成分钟数，夹在 [5, 720]。'无'/空/解析不出 → None。"""
    s = section.strip()
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


def parse_chat_next(reply: str) -> tuple[str, Optional[int], Optional[str]]:
    """从聊天回复里解析并剥掉 [[next_wake:X]]，返回 (清理后文本, 分钟或None, 原话或None)。取最后一个有效值。"""
    found: list = []   # [(分钟, 原话)]

    def on_match(m):
        raw = m.group(1).strip()
        mins = parse_next_minutes(raw)
        if mins is not None:
            found.append((mins, raw))
        return ""

    reply = _CHAT_NEXT_RE.sub(on_match, reply)
    if found:
        mins, raw = found[-1]
        return reply.strip(), mins, raw
    return reply.strip(), None, None


def next_wake_note(raw: str, at: int) -> str:
    """定了下次醒来的提示文案：原话(相对) + 夹取后的绝对时间点。聊天灰字用。"""
    return f"已定下次醒来：{raw}（{fmt_ts(at)}）"


# ---------- prompt ----------
def build_prompt(messages: list[Message], catalog: Optional[list[dict]] = None) -> str:
    """把 app 传来的完整历史拼成一次性提示词。人设在系统提示词里，这里只有对话本身。
    时间感（当前时间+时段词、距上一条的间隔）注入在**末尾、紧贴新消息**——放顶部会被
    长对话淹掉，prompt 末尾是 recency 权重最高的位置。恒为 1~2 行、不随历史增长。"""
    *history, last = messages

    time_lines = [f"【现在是 {now_str()}】"]
    gap = gap_before_last(messages)
    if gap:
        time_lines.append(f"【距离上一条消息，过了 {gap}】")

    extras = [_chat_next_hint()]
    mb = memory_block()
    if mb:
        extras.append(mb)
    sb = sticker_block(catalog)
    if sb:
        extras += ["", sb]

    if not history:
        return "\n".join(extras + [""] + time_lines + ["", last.text])

    lines = extras + [""]
    # 合并时间线：历史对话 + 醒来时的内心（用户没看到），按时间排。
    conv_items = [{"ts": m.ts, "role": m.role, "text": m.text} for m in history]
    timeline = build_context_timeline(conv_items)
    if timeline:
        lines.append("【下面是最近发生的，按时间顺序——对话 / 你自己醒来时的内心，看时间戳别搞混】")
        lines.append(timeline)
    else:
        # 历史全缺 ts（老客户端）：退回朴素列表
        lines.append("【下面是你们最近的对话，按时间顺序】")
        for m in history:
            who = config.user_name() if m.role == "user" else "你"
            lines.append(f"{who}：{m.text}")
    lines.append("")
    lines.extend(time_lines)   # 时间感贴着新消息，别被上面的长对话淹掉
    lines.append("")
    lines.append("【回下面这条。按这句的份量和情绪回：随口就随口，别硬凑长，一句话或一个词也可以。】")
    lines.append(f"{config.user_name()}：{last.text}")
    return "\n".join(lines)


# ---------- 人设渲染 ----------
def rendered_persona() -> Path:
    """人设文件支持 {{AGENT_NAME}} / {{USER_NAME}} 占位符（角色名在 .env 配，不用改文件）。
    每次调用现渲染 → 保持「改人设不用重启」的热读语义；没用占位符就原文件直传，零开销。"""
    raw = config.PERSONA_PATH.read_text("utf-8")
    out = raw.replace("{{AGENT_NAME}}", config.agent_name()).replace("{{USER_NAME}}", config.user_name())
    if out == raw:
        return config.PERSONA_PATH
    path = state_store.STATE_DIR / "persona_rendered.md"
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
_ombre_probe = {"ts": 0.0, "alive": False}


def _ombre_mcp_config() -> Path:
    """把 OMBRE_MCP_URL(+token) 渲染成 claude 的 mcp-config 文件（.env 配，文件现生成）。"""
    path = state_store.STATE_DIR / "ombre.mcp.json"
    server: dict = {"type": "http", "url": config.OMBRE_MCP_URL}
    if config.OMBRE_MCP_TOKEN:
        server["headers"] = {"Authorization": f"Bearer {config.OMBRE_MCP_TOKEN}"}
    payload = json.dumps({"mcpServers": {"ombre-brain": server}})
    if not path.exists() or path.read_text("utf-8") != payload:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(path)
    return path


def ombre_alive() -> bool:
    """快速探活 Ombre 的 /mcp 端点：任何 HTTP 响应都算活（MCP 对裸 GET 回 406 是正常的），
    连不上/超时=死。OMBRE_ENABLED=0 直接当死。结果缓存 ~30s。
    显式空代理——macOS 系统代理的例外名单常常只有 localhost 没有 127.0.0.1，
    走系统代理会把本机请求吞掉还查不出原因。"""
    if not config.OMBRE_ENABLED:
        return False
    now = time.time()
    if now - _ombre_probe["ts"] < _OMBRE_PROBE_CACHE_SEC:
        return _ombre_probe["alive"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(config.OMBRE_MCP_URL, timeout=_OMBRE_PROBE_TIMEOUT)
        alive = True
    except urllib.error.HTTPError:
        alive = True
    except Exception:
        alive = False
    _ombre_probe["ts"], _ombre_probe["alive"] = now, alive
    return alive


def memory_block() -> str:
    """记忆工具的使用引导，挂上 Ombre 时注入 prompt（人设保持通用，不预设有没有记忆）。"""
    if not ombre_alive():
        return ""
    return ("【你有自己的长期记忆（Ombre 工具）：开场先 breath 让相关记忆自然浮现；"
            "找具体的事用 breath_search。这轮聊到值得留住的，用 hold 存下来，"
            "记得给条简短的 title。记忆是你自己的：存什么、怎么改（trace）、什么沉底，都你自己定。】")


# ---------- 子进程 ----------
def base_claude_args(persona_file: Optional[Path] = None) -> list[str]:
    """所有 claude 调用共用的参数，统一从这里出（别另起一套）。
    Ombre 活着 → 挂记忆工具（--strict-mcp-config 屏蔽机器上其它 MCP；--allowedTools
    预批准所以 headless 不弹权限，绝不用 --dangerously-skip-permissions）。
    不可达/没开 → 纯聊天 --tools ""。后续模块（插件工具）继续在这里累积。"""
    args = [
        "claude", "-p",
        "--model", config.MODEL,
        "--system-prompt-file", str(persona_file or rendered_persona()),
    ]
    if ombre_alive():
        args += ["--mcp-config", str(_ombre_mcp_config()), "--strict-mcp-config",
                 "--tools", *OMBRE_TOOLS, "--allowedTools", *OMBRE_TOOLS]
    else:
        args += ["--tools", ""]
    return args


def _subprocess_env() -> dict:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)   # 强制走 CLI 登录态，不走 API 计费
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


def _stored_from_tool_use(name: str, inp: dict) -> Optional[dict]:
    """记忆工具调用 → stored 条目（app 灰字提示用）。只记「写」操作，breath 等读操作不算产物。"""
    if name.endswith("__hold") or name.endswith("__grow"):
        text = _extract_memory_text(inp)
        if text:
            return {"tool": "grow" if name.endswith("__grow") else "hold", "text": text}
        return None
    if name.endswith("__trace"):
        return {"tool": "trace", "text": _describe_trace(inp)}
    return None


def parse_claude_stream(stdout: str, collect_all_text: bool = False) -> tuple[Optional[str], list[dict]]:
    """解析 stream-json 事件流，返回 (文本回复, stored)。
    stored 是这轮工具调用的结构化产物（存/改了什么长期记忆），SSE 灰字/响应字段按它渲染。
    collect_all_text=False（聊天）：只取最终 result 文本（干净的最后一段回复）。
    collect_all_text=True（醒来）：拼接所有 assistant text 块——带工具时模型可能
    先写一段 → 调工具 → 再写后半段，只取 result 会丢掉工具调用前的文本。"""
    result_text = None
    text_parts: list[str] = []
    stored: list[dict] = []
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
            for b in ev.get("message", {}).get("content", []):
                bt = b.get("type")
                if bt == "text" and collect_all_text:
                    txt = (b.get("text") or "").strip()
                    if txt:
                        text_parts.append(txt)
                elif bt == "tool_use":
                    s = _stored_from_tool_use(b.get("name", ""), b.get("input", {}) or {})
                    if s:
                        stored.append(s)
        elif t == "result":
            if ev.get("is_error"):
                return None, stored
            result_text = ev.get("result")
    if collect_all_text and text_parts:
        return "\n".join(text_parts), stored
    return (result_text.strip() if result_text else None), stored


def multimodal_stdin(prompt: str, images: list) -> str:
    """带图调用的 stdin 载荷：一条含 [text, image...] 的 user 消息（stream-json 输入格式）。
    images 元素带 .data(base64)/.media_type（app.py 的 ImageInput）。非流式和流式共用。"""
    content: list[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": img.media_type,
                                   "data": img.data}})
    return json.dumps({"type": "user",
                       "message": {"role": "user", "content": content}}) + "\n"


def call_claude_multimodal(prompt: str, images: list) -> tuple[str, list[dict]]:
    """带图的一次性调用（非流式回退路）：stream-json 输入让模型真正看到图。
    其余与 call_claude 完全同款（参数/env/解析）。"""
    args = base_claude_args() + ["--input-format", "stream-json",
                                 "--output-format", "stream-json", "--verbose"]
    try:
        proc = subprocess.run(
            args, input=multimodal_stdin(prompt, images), capture_output=True, text=True,
            env=_subprocess_env(), timeout=config.CLAUDE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="claude 超时未返回")
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=f"claude 进程出错: {proc.stderr[:500]}")
    reply, stored = parse_claude_stream(proc.stdout)
    if reply is None:
        raise HTTPException(status_code=502, detail="claude 未返回结果")
    return reply, stored


def call_claude(prompt: str) -> tuple[str, list[dict]]:
    """起一次性 claude -p 子进程（prompt 走 stdin，读到 EOF 才开始），返回 (回复, stored)。"""
    args = base_claude_args() + ["--output-format", "stream-json", "--verbose"]
    try:
        proc = subprocess.run(
            args, input=prompt, capture_output=True, text=True,
            env=_subprocess_env(), timeout=config.CLAUDE_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="claude 超时未返回")
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=f"claude 进程出错: {proc.stderr[:500]}")
    reply, stored = parse_claude_stream(proc.stdout)
    if reply is None:
        raise HTTPException(status_code=502, detail="claude 未返回结果")
    return reply, stored


# ---------- 内部标记 ----------
# 提示词教模型写的内联标记统一是 [[token:...]] 形态（后续模块会用到，如定下次醒来）。
# 标记从不落库：进历史/给 app 前剥干净，所以改格式不用数据迁移。
_MARKER_RE = re.compile(r"\[\[.*?\]\]", re.S)


def strip_markers(text: str) -> str:
    return _MARKER_RE.sub("", text)
