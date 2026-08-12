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
    """把 最近对话 + 模型自己醒来时的内心（不在聊天里，用户在心流日志页看得到）合并成
    一条按时间排序的时间线。
    醒来和聊天共用——解决「对话本身没时间戳、和内心对不上先后顺序」的问题。"""
    items: list[tuple[int, str]] = []

    for c in conv_items:
        ts = c.get("ts")
        if ts is None:
            continue
        who = config.user_name() if c.get("role") == "user" else "你"
        items.append((int(ts), f"{who}：{c.get('text', '')}"))

    # 最近几次醒来的内心（含 none；不在聊天里，心流日志页可见）。只读日志尾部——append-only 文件会一直长。
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


def pronoun_hint() -> str:
    """人称代词提示（聊天/醒来共用注入）：不给的话模型会自己猜用户性别，猜错很伤。"""
    return f"【提到{config.user_name()}时，人称代词一律用「{config.user_pronoun()}」——写记忆、内心独白也一样。】"


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
def build_prompt(messages: list[Message], catalog: Optional[list[dict]] = None,
                 char_id: Optional[str] = None) -> str:
    """把 app 传来的完整历史拼成一次性提示词。人设在系统提示词里，这里只有对话本身。
    时间感（当前时间+时段词、距上一条的间隔）注入在**末尾、紧贴新消息**——放顶部会被
    长对话淹掉，prompt 末尾是 recency 权重最高的位置。恒为 1~2 行、不随历史增长。"""
    *history, last = messages

    time_lines = [f"【现在是 {now_str()}】"]
    gap = gap_before_last(messages)
    if gap:
        time_lines.append(f"【距离上一条消息，过了 {gap}】")

    extras = [pronoun_hint(), _chat_next_hint()]
    mb = memory_block(char_id)
    if mb:
        extras.append(mb)
    sb = sticker_block(catalog)
    if sb:
        extras += ["", sb]

    if not history:
        return "\n".join(extras + [""] + time_lines + ["", last.text])

    lines = extras + [""]
    # 合并时间线：历史对话 + 醒来时的内心（不在聊天里，心流日志页可见），按时间排。
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


def memory_block(char_id: Optional[str] = None) -> str:
    """记忆工具的使用引导，挂上 Ombre 时注入 prompt（人设保持通用，不预设有没有记忆）。"""
    if not ombre_alive(char_id):
        return ""
    return ("【你有自己的长期记忆（Ombre 工具）：开场先 breath 让相关记忆自然浮现；"
            "找具体的事用 breath_search。这轮聊到值得留住的，用 hold 存下来，"
            "记得给条简短的 title。记忆是你自己的：存什么、怎么改（trace）、什么沉底，都你自己定。"
            "存新记忆用普通 hold；feel=True 是给一条已存在的记忆挂情绪批注，source_bucket 必填。"
            "想凭空记一份新心情：先普通 hold 存下来、拿到它的 id，再 hold(feel=True, "
            "source_bucket=那个id)——两步。空着 source_bucket 调 feel 一定失败。】")


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
    mcp_configs: list[str] = []
    tools: list[str] = []
    if ombre_alive(char_id):
        mcp_configs.append(str(_ombre_mcp_config(char_id)))
        tools += OMBRE_TOOLS
    plug_cfg, plug_tools = plugins.mounted(context, char_id)
    if plug_cfg:
        mcp_configs.append(plug_cfg)
        tools += plug_tools
    if tools:
        for c in mcp_configs:
            args += ["--mcp-config", c]
        args += ["--strict-mcp-config", "--tools", *tools, "--allowedTools", *tools]
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


def tool_result_error(block: dict) -> Optional[str]:
    """这条 tool_result 算失败吗？是则返回给人看的原因，否则 None。三道判据：
      ① is_error：MCP 标准错误（参数校验不过之类）；
      ② 返回体是 {"ok": false, "error": ...}：插件的口径（codemode 就这么回）；
      ③ 正文命中婉拒名单：Ombre 那种 isError:false 的软拒绝。
    ①② 是结构判据、准；③ 是文本判据、只认见过的说法（见 _TOOL_REJECT_MARKS）。"""
    text = _tool_result_text(block).strip()
    if block.get("is_error") or block.get("isError"):
        return _short_reason(text) or "工具报错了"
    try:
        obj = json.loads(text)
    except Exception:
        obj = None
    if isinstance(obj, dict) and obj.get("ok") is False:
        return _short_reason(str(obj.get("error") or "")) or "没成功"
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


def multimodal_stdin(prompt: str, images: list, file_blocks: Optional[list[dict]] = None) -> str:
    """带图/文件调用的 stdin 载荷：一条含 [text, image, document...] 的 user 消息
    （stream-json 输入格式）。images 元素带 .data(base64)/.media_type（app.py 的
    ImageInput）；file_blocks 是 app.py _file_to_block 转好的 document block。"""
    content: list[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": img.media_type,
                                   "data": img.data}})
    content.extend(file_blocks or [])
    return json.dumps({"type": "user",
                       "message": {"role": "user", "content": content}}) + "\n"


def call_claude_multimodal(prompt: str, images: list,
                           file_blocks: Optional[list[dict]] = None,
                           char_id: Optional[str] = None) -> tuple[str, list[dict]]:
    """带图/文件的一次性调用（非流式回退路）：stream-json 输入让模型真正看到。
    其余与 call_claude 完全同款（参数/env/解析）。"""
    args = base_claude_args(char_id=char_id) + ["--input-format", "stream-json",
                                                "--output-format", "stream-json", "--verbose"]
    try:
        proc = subprocess.run(
            args, input=multimodal_stdin(prompt, images, file_blocks),
            capture_output=True, text=True,
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


def call_claude(prompt: str, char_id: Optional[str] = None) -> tuple[str, list[dict]]:
    """起一次性 claude -p 子进程（prompt 走 stdin，读到 EOF 才开始），返回 (回复, stored)。"""
    args = base_claude_args(char_id=char_id) + ["--output-format", "stream-json", "--verbose"]
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

    return _BROWSER_CHOICE_RE.sub(_on, text), choice
