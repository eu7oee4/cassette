"""chat_loop：常驻聊天引擎（PLAN_sdk S2/PR10a——agent-sdk 每角色一条 session + forge）。

架构（设计定稿在 PLAN_sdk §10 S2 两份设计稿）：
- 权威=手机消息历史（每次请求全量带 req.messages），transcript 只是铸出来的缓存，
  recent_window 照旧只服务 wake，不参与这里的任何判断。
- **判脏=发送前比对**：已铸账（本 session 铸过/追加过的 (role, hash) 序列）vs 本次
  请求带来的历史。一致=纯追加只注入新消息；分歧=从权威整体重铸（截尾语义 §6）。
  脏不是状态机，是比对结果——没有跨轮标记，也就没有标记丢失/竞态。
- **任何一轮没有正常收尾（finalize 没跑到）→ 关 session，下次发送惰性重起+重铸**。
  比 PR9 的「轮死≠session 死」保守一档，但零暧昧状态：账和 transcript 永不可能
  悄悄分叉——所有异常都收敛到「下一条多等几秒」。
- SSE 协议零改动：把 SDK 类型化消息转回 CLI stream-json 的 dict 形状，喂给现有
  sse.translate_events（text_break/MarkerStreamFilter/StoredCollector 原样复用）。

红线：铸造输入只有权威窗口（req.messages）——绝不读回 transcript；tick/注入
包装文字（时间感/叮嘱）不进账，重铸时按权威原文重渲染，包装自然脱落。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent, ToolResultBlock, UserMessage

import config
import forge
import session_mgr
import sse
import state_store
from forge import estimate_tokens

SCENE = "chat"
LEDGER_DIR = state_store.STATE_DIR / "chat_sessions"
# 浅层错位容忍：请求发出时上一轮回复还没落进 app 历史（连发竞态）/wake 气泡还没
# 被 app 拉走——账尾多出的**纯 assistant** 条目不算脏。超过这个深度按脏处理。
STALE_TOLERANCE = 8

# ---- chat 重铸节奏（§4 chat 条，08-30 拍板）----
# 时机轴只有一条：TA 静默 ≥1h 的轮间隙——对话进行中永不铸（缓存正值钱）；
# 1h 恰好是缓存 TTL 边界，过了它缓存横竖已死，此时铸的缓存成本严格为零。
# 压力轴现在只有「窗口超软阈」一条可用（脏走发送前比对；可折活动段等 PR13）。
# 硬阈=马拉松对话没间隙也得铸（下个轮尾强铸）——聊天正文**绝不退** harness
# auto-compact（摘要压缩=信感复发）。门槛三值施工中调（plan 原话）。
CHAT_REFORGE_IDLE_SEC = int(os.environ.get("CHAT_REFORGE_IDLE_SEC", "3600"))
CHAT_SOFT_TOKENS = int(os.environ.get("CHAT_SOFT_TOKENS", "100000"))
CHAT_HARD_TOKENS = int(os.environ.get("CHAT_HARD_TOKENS", "150000"))
IDLE_CHECK_SEC = 60           # 泵在轮间隙醒来看一眼的周期
CONSOLIDATE_TIMEOUT = 300

# SDK 聊天路的熄火开关（连败 3 次自动回 -p，见 app._engine_chunks）。放这儿不放
# app.py：wake_sdk 分路也要认它——聊天路都熄了火，醒来还往 session 里塞就是往
# 一个起不来的引擎里灌（计数器 _SDK_CHAT_FAILS 仍归 app，它只在请求路上加减）。
SDK_CHAT_OFF: set[str] = set()

# 巩固钩子（§5.4 睡眠周期纪律；措辞感知式·无感，不点破重铸）。产物不投递——
# 独处时的整理，没对 TA 说话；重铸后这轮从 transcript 消失，人不记得「记住」
# 这个动作本身，前提是内容真进了 Ombre（hold）。
CONSOLIDATE_PROMPT = ("〔安静下来了。趁这会儿把最近聊的过一遍，值得留住的用 hold "
                      "存好——回忆就好，不用说话。〕")
# 开局引子（§5.4① 开局包的 breath 半边；行为清单半边等活动账本落地 PR13）。
# 动作式、无条件——2026-08-13 教训：「想不起来时再搜」= 最需要搜的时候恰恰不觉得需要。
OPENING_NUDGE = "〔接话之前先 breath 一下，把最近的记忆过一遍。〕"


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def norm_history(messages: list[dict]) -> list[dict]:
    """权威窗口标准化：只留 role/text/ts、滤空气泡（forge.render 也会拒空）。"""
    out = []
    for m in messages:
        t = (m.get("text") or "")
        if t.strip() and m.get("role") in ("user", "assistant"):
            out.append({"role": m["role"], "text": t, "ts": m.get("ts")})
    return out


def divergence(ledger: list[dict], history: list[dict]) -> Optional[str]:
    """None=干净（纯追加）；"stale"=账尾多出纯 assistant（窗口没跟上，不脏）；
    "dirty"=编辑/删除/账亡，需要重铸。
    干净判据：history 恰好等于 ledger 的尾部切片（窗口滑动天然满足——账只会比窗长）。
    """
    led = [(e["r"], e["h"]) for e in ledger]
    hist = [(m["role"], _h(m["text"])) for m in history]
    if not led:
        return "dirty" if hist else None
    if not hist:
        return "dirty"   # 账里有货、窗口全空=app 清了历史，必须重铸
    for drop in range(0, min(STALE_TOLERANCE, len(led)) + 1):
        end = len(led) - drop
        start = end - len(hist)
        if start < 0 or end <= 0:
            break
        if led[start:end] == hist:
            if drop == 0:
                return None
            if all(r == "assistant" for r, _ in led[end:]):
                return "stale"
            return "dirty"
    return "dirty"


@dataclass
class Turn:
    """一次注入。out 收 SSE bytes，None 收尾——每个 Turn 恰好一个结局
    （done 正常走完 ∣ error+done），补投/error 盒仍归 app.py 的 rescue 观察者。

    kind="wake"（PR12 醒来升 A）：没有请求方——out 没人读、history/new_msg 空着；
    注入由 injection_factory 在**执行开始时**组装（排队几分钟后时间那句不能是死的）；
    finalize 里做投递（wake_sdk.finish_wake_turn），返回的 reply=真投递出去的正文
    （空串=安静醒着，账不动）；轮没走完 → on_dead（记 error 心流+冷却）。"""
    rid: str
    history: list[dict]                  # 权威窗口（不含新消息），已 norm
    new_msg: dict                        # {"role":"user","text","ts"}（入账用原文）
    injection: str                       # 实际注入的包装文本（不进账）
    finalize: Callable[[str, list], dict]
    images: Optional[list] = None
    file_blocks: Optional[list] = None
    catalog: Optional[list] = None       # 表情目录：重铸/起 session 时进系统提示
    kind: str = "chat"                   # "chat" | "wake"（轮来源标签，投递路由靠它）
    injection_factory: Optional[Callable[[], str]] = None
    on_dead: Optional[Callable[[], None]] = None
    out: asyncio.Queue = field(default_factory=asyncio.Queue)


# ---------- options ----------

def _merge_mcp_file(servers: dict, cfg_path: str) -> None:
    try:
        data = json.loads(open(cfg_path, encoding="utf-8").read())
        servers.update(data.get("mcpServers", {}))
    except Exception as e:
        print(f"[chat_loop] MCP 配置解析失败（跳过 {cfg_path}）: {e}", file=sys.stderr)


# 醒来轮契约（§0.3：注入只给知觉材料，行为契约在系统提示里一次性写死）。
# 〔〕外=会发到 TA 手机上的消息、〔〕内=心里活动只留档——投递分流在
# wake_sdk.split_musings，这里的字必须和那边的机械口径一字不差地对上。
def _wake_contract() -> str:
    u = config.user_name()
    return (f"【有时你会自己醒来：没有{u}的新消息，只有一段〔〕包着的知觉（时间、间隔、"
            f"见闻）。那不是{u}在找你，是你自己的时间——想干什么干什么，不用非得说话。"
            f"这种醒来的时候，你写在〔〕外的话会作为消息发到{u}手机上；只想自己待着，"
            f"就把心里活动整段用〔〕包起来（包着的不会发出去），没什么可说就安安静静"
            f"待着，别硬找话。】")


def _wake_gate(handle: session_mgr.LoopHandle):
    """PreToolUse 门：醒来轮的禁用面（§5.2 轮来源标签驱动）。
    走 hook 不走 can_use_tool——allowed_tools 的整工具条目会在回调之前自动放行
    （SDK 实证：_warn_if_can_use_tool_shadowed），hook 在权限判定之前跑，拦得住。
    turn_kind 不是 wake（聊天轮/巩固轮）→ 全放行，行为与没挂 hook 一字不差。"""
    async def gate(hook_input, tool_use_id, ctx) -> dict:
        if handle.meta.get("turn_kind") != "wake":
            return {}
        tool = (hook_input or {}).get("tool_name") or ""
        if tool in (handle.meta.get("wake_tools") or set()):
            return {}
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "这会儿是你自己醒着的时间，这个工具不在手边（醒来那条路不挂它）"
                "——想用的话留到聊天或上机的时候。"),
        }}
    return gate


def build_options(char_id: str, catalog: Optional[list] = None,
                  handle: Optional[session_mgr.LoopHandle] = None) -> ClaudeAgentOptions:
    """口径对齐 pipeline.base_claude_args（chat 路）：人设+菜单+叮嘱进系统提示，
    Ombre http 直传 dict，插件/宠物/skills 的 mcp.json 解析合并，白名单+strict。
    与 -p 的差别只有结构：稳定段从每轮 prompt 挪进 session 系统提示（付一次）。
    handle＝泵的把手（PR12）：给了才挂醒来禁用面的 PreToolUse 门（按 meta.turn_kind
    判轮）；测试/工具脚本不传，拿到的 options 和从前一样。"""
    import pipeline
    if os.environ.get("ANTHROPIC_API_KEY"):
        # -p 那条路每次 pop 掉 key；SDK 子进程继承我们的 env，没法逐调用摘——
        # 有 key 就拒开，绝不静默改成 API 计费（订阅纪律）。
        raise RuntimeError("环境里有 ANTHROPIC_API_KEY：SDK 聊天路会走 API 计费，拒开")

    parts = [pipeline.rendered_persona(char_id).read_text("utf-8")]
    mb = pipeline.tool_menu_block("chat", char_id)
    if mb:
        parts.append(mb)
    parts += [pipeline.pronoun_hint(), pipeline._chat_next_hint(), _wake_contract()]
    sb = pipeline.sticker_block(catalog)
    if sb:
        parts.append(sb)
    system = "\n\n".join(p for p in parts if p)

    servers: dict = {}
    tools: list[str] = []
    if pipeline.ombre_alive(char_id):
        import characters
        oc = characters.ombre_conf(char_id)
        server: dict = {"type": "http", "url": oc["mcp_url"]}
        if oc["mcp_token"]:
            server["headers"] = {"Authorization": f"Bearer {oc['mcp_token']}"}
        servers["ombre-brain"] = server
        tools += pipeline.OMBRE_TOOLS
    import plugins
    plug_cfg, plug_tools = plugins.mounted("chat", char_id)
    if plug_cfg:
        _merge_mcp_file(servers, plug_cfg)
        tools += plug_tools
    if pipeline._pet_mcp_mounted(char_id):
        _merge_mcp_file(servers, str(pipeline._pet_mcp_config(char_id)))
        tools += pipeline.PET_MCP_TOOLS
    if pipeline._skills_mounted("chat", char_id):
        import skills
        _merge_mcp_file(servers, str(skills.mcp_config(char_id)))
        tools += skills.SKILLS_MCP_TOOLS

    env = {}
    if tools and pipeline.tool_search_on("chat"):
        tools = tools + [pipeline.TOOL_SEARCH_TOOL]
        env["ENABLE_TOOL_SEARCH"] = "true"

    return ClaudeAgentOptions(
        system_prompt=system,
        model=config.MODEL,
        cwd=pipeline.neutral_cwd(),
        mcp_servers=servers,
        strict_mcp_config=True,
        tools=tools,
        allowed_tools=list(tools),
        env=env,
        include_partial_messages=True,   # 逐字增量：text 事件靠它
        # max_turns 不设：-p 路从来没限过，聊天轮的工具链长度由模型自己收
        hooks=({"PreToolUse": [HookMatcher(hooks=[_wake_gate(handle)])]}
               if handle is not None else None),
    )


# ---------- 注入正文（易变段；稳定段在系统提示里） ----------

def build_injection(messages, char_id: Optional[str],
                    extra_hints: Optional[list[str]] = None) -> str:
    """一轮的包装文本：叮嘱+待办+时间感+新消息。口径抄 build_prompt 的易变段——
    历史不在这儿（历史活在 transcript 里，这正是整个迁移的意义）。"""
    import pipeline
    last = messages[-1]
    lines = [h for h in (extra_hints or []) if h]
    lines.append(pipeline.one_turn_hint("chat_session"))
    pending = pipeline.pending_todo_block(char_id)
    if pending:
        lines.append(pending)
    lines.append("")
    lines.append(f"【现在是 {pipeline.now_str()}】")
    gap = pipeline.gap_before_last(messages)
    if gap:
        lines.append(f"【距离上一条消息，过了 {gap}】")
    lines.append("")
    lines.append("【回下面这条。按这句的份量和情绪回：随口就随口，别硬凑长，"
                 "一句话或一个词也可以。】")
    lines.append(f"{config.user_name()}：{last.text}")
    return "\n".join(lines)


# ---------- SDK 消息 → CLI stream-json dict（sse.translate_events 的口粮） ----------

def _assistant_dict(msg: AssistantMessage) -> dict:
    blocks = []
    for b in msg.content:
        if isinstance(b, TextBlock):
            blocks.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name,
                           "input": b.input})
    return {"type": "assistant", "message": {"content": blocks}}


def _user_dict(msg: UserMessage) -> dict:
    blocks = []
    content = msg.content if isinstance(msg.content, list) else []
    for b in content:
        if isinstance(b, ToolResultBlock):
            blocks.append({"type": "tool_result", "tool_use_id": b.tool_use_id,
                           "content": b.content, "is_error": b.is_error})
    return {"type": "user", "message": {"content": blocks}}


async def _turn_events(client, handle: session_mgr.LoopHandle,
                       flags: dict) -> AsyncIterator[dict]:
    """吃一轮（到 ResultMessage 止），产出 translate_events 认识的事件 dict。
    空闲超时/流断都产 __idle_timeout__ 哨兵（translate 会以 error+done 收尾），
    并在 flags 里留痕给泵决定 session 生死。"""
    agen = client.receive_messages().__aiter__()
    while True:
        try:
            msg = await asyncio.wait_for(agen.__anext__(),
                                         timeout=config.CLAUDE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            flags["timeout"] = True
            print("[chat_loop] 轮内空闲超时（session 按死处理）", file=sys.stderr)
            yield {"type": "__idle_timeout__"}
            return
        except StopAsyncIteration:
            flags["eof"] = True
            print("[chat_loop] 引擎消息流断了（session 按死处理）", file=sys.stderr)
            yield {"type": "__idle_timeout__"}
            return
        if isinstance(msg, StreamEvent):
            yield {"type": "stream_event", "event": msg.event}
        elif isinstance(msg, AssistantMessage):
            handle.touch()
            yield _assistant_dict(msg)
        elif isinstance(msg, UserMessage):
            yield _user_dict(msg)
        elif isinstance(msg, ResultMessage):
            _note_usage(handle.char_id, msg,
                        handle.meta.get("turn_kind") or "chat")
            if msg.is_error:
                flags["error_subtype"] = msg.subtype or "?"
            yield {"type": "result", "result": msg.result,
                   "is_error": msg.is_error, "subtype": msg.subtype,
                   "api_error_status": msg.api_error_status}
            return


def _note_usage(char_id: str, msg: ResultMessage, kind: str = "chat") -> None:
    """usage 落账（PLAN_sdk §10 S2 设计稿二：手机端 usage 面板的数据源）。
    append-only jsonl 按天分文件；写失败只记日志，绝不影响聊天轮。
    kind＝轮来源（chat/wake）：面板上「他自己醒来花的」和「陪 TA 聊花的」分得开。"""
    try:
        d = state_store.STATE_DIR / "usage"
        d.mkdir(exist_ok=True)
        rec = {"ts": int(time.time()), "char": char_id,
               "scene": SCENE if kind == "chat" else f"{SCENE}/{kind}",
               "subtype": msg.subtype, "usage": msg.usage or {}}
        day = time.strftime("%Y%m%d")
        with open(d / f"usage-{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[chat_loop] usage 落账失败: {e}", file=sys.stderr)


# ---------- 账 ----------

def _ledger_path(char_id: str):
    return LEDGER_DIR / f"{char_id}.json"


def _persist_ledger(char_id: str, sid: Optional[str], ledger: list[dict]) -> None:
    """观测用落盘（真相永远是权威消息库；重启后一律视同账亡重铸，不从这儿恢复）。"""
    try:
        LEDGER_DIR.mkdir(exist_ok=True)
        tmp = _ledger_path(char_id).with_suffix(".tmp")
        tmp.write_text(json.dumps({"sid": sid, "ledger": ledger},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(_ledger_path(char_id))
    except Exception as e:
        print(f"[chat_loop] 账落盘失败: {e}", file=sys.stderr)


# ---------- 活动框（§4 规则二：回流点评带上「你当时在读剧情」的框架）----------

def _frame_activities(history: list[dict], char_id: str) -> list[dict]:
    """给权威窗口里落在活动区间内的消息连段加两行文档框（user 槽感知式，
    世界事件走文档侧，红线合法）。**只改铸造输入，不改已铸账**——账永远对
    权威原文，框行是渲染层的选择（§4：折叠/带不带是渲染规则，字面不动）。
    确定性：框行内容只由区间数据派生，同输入同字节。"""
    if not history:
        return history
    import activity_log
    intervals = activity_log.read_intervals(
        char_id, since_ts=int(history[0].get("ts") or 0) - 60)
    if not intervals:
        return history

    def _hm(ts: int) -> str:
        return time.strftime("%H:%M", time.localtime(ts))

    out: list[dict] = []
    i, n = 0, len(history)
    for iv in intervals:
        s, e = int(iv["start"]), int(iv["end"])
        note = iv.get("note") or "游戏"
        while i < n and int(history[i].get("ts") or 0) < s:
            out.append(history[i])
            i += 1
        j = i
        while j < n and int(history[j].get("ts") or 0) <= e:
            j += 1
        if j > i:
            out.append({"role": "user", "ts": s,
                        "text": f"〔{_hm(s)} 你开了{note}会话，下面这些是你边读边说的〕"})
            out.extend(history[i:j])
            out.append({"role": "user", "ts": e,
                        "text": f"〔{_hm(e)} 这一场到这儿收了摊〕"})
            i = j
    out.extend(history[i:])
    return out


# ---------- 见闻（记忆 vs 见闻轴的见闻侧，§5.2）----------

def _ombre_on(char_id: str) -> bool:
    import pipeline
    try:
        return pipeline.ombre_alive(char_id)
    except Exception:
        return False


def _seen_block(char_id: str, since_ts: int) -> tuple[Optional[str], int]:
    """见闻增量：醒来内心 + 小屋经历里 ts>since_ts 的部分（渲染口径同
    build_context_timeline，pipeline.seen_items 一份两用）。返回 (文本或 None, 新游标)。
    since_ts=0（session 刚开/重铸后）＝开局快照：最近几条全给，对齐 -p 路每轮
    时间线里的非对话内容——重铸丢掉的旧见闻由此换成新鲜的（§5.4 红线：
    别把过期档案固化成假新鲜）。"""
    import pipeline
    import world
    exp_n = world.experience_limit() if world.house_active() else 0
    items = pipeline.seen_items(char_id, reflect_limit=5,
                                experience_limit=exp_n, since_ts=since_ts)
    if not items:
        return None, since_ts
    cursor = max(ts for ts, _ in items)
    head = ("【这期间的见闻——你醒来时的内心 / 你在小屋里看见的（带（房间名）前缀），"
            "不是聊天消息】" if exp_n else
            "【这期间的见闻——你自己醒来时的内心，不是聊天消息】")
    body = "\n".join(f"[{pipeline.fmt_ts(ts)}] {t}" for ts, t in items)
    return head + "\n" + body, cursor


# ---------- 泵 ----------

async def _safe_disconnect(client) -> None:
    if client is None:
        return
    try:
        await client.disconnect()
    except BaseException:
        pass


async def run(handle: session_mgr.LoopHandle, *,
              client_factory: Optional[Callable] = None,
              options_factory: Optional[Callable] = None) -> None:
    """泵本体（session_mgr 的 runner）：串行吃 Turn。每轮流程=
    判脏（比对账 vs 本次权威窗口）→（脏则关旧+重铸+resume）→ 注入 → 翻译产出 SSE
    → finalize 跑到才入账。任何一轮没正常收尾 → 关 session（下一条惰性重起）。"""
    factory = client_factory or ClaudeSDKClient
    # 默认 factory 闭包带上 handle：PreToolUse 醒来门要按 meta.turn_kind 判轮。
    # 测试注入的 options_factory 保持 (char_id, catalog) 老签名，不用跟着改。
    opts_factory = options_factory or (
        lambda cid, cat: build_options(cid, cat, handle=handle))
    client = None
    sid: Optional[str] = None
    ledger: list[dict] = []
    handle.meta["ledger"] = ledger    # 测试/观测窗口

    async def _open_session(history: list[dict], catalog):
        nonlocal client, sid, ledger
        await _safe_disconnect(client)
        client = None
        options = opts_factory(handle.char_id, catalog)
        if history:
            sid = forge.render(_frame_activities(history, handle.char_id),
                               cwd=str(options.cwd),
                               model=config.MODEL)
            options = copy.copy(options)
            options.resume = sid
        else:
            sid = None
        c = factory(options)
        await c.connect()
        ledger = [{"r": m["role"], "h": _h(m["text"]), "ts": m.get("ts")}
                  for m in history]
        handle.meta["ledger"] = ledger
        handle.meta["ctx_est"] = sum(estimate_tokens(m["text"]) for m in history)
        handle.meta["seen_cursor"] = 0        # 开局重发新鲜见闻快照
        handle.meta["needs_opening"] = True   # 下一轮带开局引子（breath）
        handle.last_reopen = time.time()
        client = c

    async def _consolidate_and_reforge(why: str, catalog) -> None:
        """轮间隙重铸：巩固钩子轮（hold；产物不投递——独处时的整理）→ 从
        recent_window 镜像重铸。镜像与手机权威的任何漂移由下次发送的比对兜住
        （自愈：顶多多铸一次）。§5.4 的「巩固产物确认写成功才允许重铸」目前是
        软版（巩固轮收尾即铸，hold 成没成功不查）——严格版记在 plan 待办。"""
        nonlocal client, ledger
        if client is None:
            return
        print(f"[chat_loop] 轮间隙重铸（char={handle.char_id}，{why}）",
              file=sys.stderr)
        handle.reopening = True
        try:
            if _ombre_on(handle.char_id):
                await client.query(CONSOLIDATE_PROMPT)
                flags: dict = {}
                async for _ in _turn_events(client, handle, flags):
                    pass
                if flags.get("timeout") or flags.get("eof"):
                    raise RuntimeError(f"巩固轮没收尾: {flags}")
            history = norm_history(state_store.read_recent_window(handle.char_id))
            if not history:
                print("[chat_loop] 镜像空白，重铸放弃（下次发送惰性处理）",
                      file=sys.stderr)
                return
            await _open_session(history, catalog)
        finally:
            handle.reopening = False

    try:
        while True:
            try:
                turn: Turn = await asyncio.wait_for(handle.queue.get(),
                                                    timeout=IDLE_CHECK_SEC)
            except asyncio.TimeoutError:
                # 轮间隙看一眼：静默 ≥1h × 窗口超软阈 → 巩固+重铸（§4 chat 节奏；
                # 铸完 ctx_est 回到纯对话体量，自然不会连环触发）
                if (client is not None
                        and time.time() - handle.last_activity >= CHAT_REFORGE_IDLE_SEC
                        and int(handle.meta.get("ctx_est", 0)) > CHAT_SOFT_TOKENS):
                    try:
                        await _consolidate_and_reforge("静默间隙+软阈",
                                                       handle.meta.get("catalog"))
                    except Exception as e:
                        print(f"[chat_loop] 轮间隙重铸失败，session 关掉惰性重起: {e}",
                              file=sys.stderr)
                        await _safe_disconnect(client)
                        client = None
                        ledger = []
                        handle.meta["ledger"] = ledger
                continue
            handle.touch()
            ok = False
            try:
                if turn.kind == "wake":
                    # 醒来轮（PR12）：没有请求方权威窗口——session 活着就直接注入
                    # （账 vs app 的漂移由 TA 下一条消息的发送比对兜住，自愈）；
                    # 没开就从镜像惰性开一个（「由 wake 唤起 resume」归这里）。
                    if client is None:
                        hist = norm_history(
                            state_store.read_recent_window(handle.char_id))
                        cat = turn.catalog if turn.catalog is not None else (
                            handle.meta.get("catalog")
                            or state_store.read_sticker_catalog())
                        await _open_session(hist, cat)
                    handle.meta["turn_kind"] = "wake"
                    # 醒来禁用面：本轮允许集=醒来那条路实际挂载的工具
                    # （四档策略/独占归属/探活全在 mounted_tool_names 里算过了）。
                    import pipeline
                    handle.meta["wake_tools"] = set(
                        pipeline.mounted_tool_names("wake", handle.char_id))
                else:
                    verdict = divergence(ledger, turn.history) if client else "dirty"
                    if verdict == "dirty":
                        if client:
                            print(f"[chat_loop] 判脏（char={handle.char_id}），重铸",
                                  file=sys.stderr)
                        await _open_session(turn.history, turn.catalog)
                    handle.meta["turn_kind"] = "chat"
                if turn.catalog is not None:
                    handle.meta["catalog"] = turn.catalog   # 轮间隙重铸要用的最近目录
                if turn.injection_factory is not None:
                    turn.injection = turn.injection_factory()
                # ---- 注入这一轮（开局引子 + 见闻增量 + 包装文本；都不进账）----
                parts: list[str] = []
                if handle.meta.pop("needs_opening", False) and _ombre_on(handle.char_id):
                    parts.append(OPENING_NUDGE)
                seen, cur = _seen_block(handle.char_id,
                                        int(handle.meta.get("seen_cursor", 0)))
                if seen:
                    parts.append(seen)
                    handle.meta["seen_cursor"] = cur
                injection = ("\n\n".join(parts + [turn.injection])
                             if parts else turn.injection)
                content: list[dict] = [{"type": "text", "text": injection}]
                for img in (turn.images or []):
                    content.append({"type": "image",
                                    "source": {"type": "base64",
                                               "media_type": img.media_type,
                                               "data": img.data}})
                content.extend(turn.file_blocks or [])

                async def _payload():
                    yield {"type": "user",
                           "message": {"role": "user", "content": content},
                           "parent_tool_use_id": None}
                await client.query(_payload())

                flags: dict = {}
                captured: dict = {}

                def _fin(reply: str, stored: list) -> dict:
                    payload = turn.finalize(reply, stored)
                    captured["finalized"] = True
                    captured["payload"] = payload
                    captured["raw"] = reply
                    captured["reply"] = payload.get("reply") or reply
                    return payload

                async for chunk in sse.translate_events(
                        _turn_events(client, handle, flags), _fin):
                    turn.out.put_nowait(chunk)

                if turn.kind == "wake":
                    if captured.get("finalized"):
                        # 投递了消息 → 账追加一条 assistant（app 拉走 outbox 后
                        # 历史里就有它）；安静醒着 → 账不动，这轮独处只活在
                        # transcript 里，下次重铸自然蒸发（§5.2 纯内心不渲染）。
                        delivered = (captured.get("payload") or {}).get("reply") or ""
                        if delivered:
                            ledger.append({"r": "assistant", "h": _h(delivered),
                                           "ts": int(time.time())})
                            _persist_ledger(handle.char_id, sid, ledger)
                        handle.meta["ctx_est"] = (int(handle.meta.get("ctx_est", 0))
                                                  + estimate_tokens(injection)
                                                  + estimate_tokens(
                                                      captured.get("raw") or ""))
                        ok = True
                elif captured.get("reply"):
                    ledger.append({"r": "user", "h": _h(turn.new_msg["text"]),
                                   "ts": turn.new_msg.get("ts")})
                    ledger.append({"r": "assistant", "h": _h(captured["reply"]),
                                   "ts": int(time.time())})
                    _persist_ledger(handle.char_id, sid, ledger)
                    handle.meta["ctx_est"] = (int(handle.meta.get("ctx_est", 0))
                                              + estimate_tokens(injection)
                                              + estimate_tokens(captured["reply"]))
                    ok = True
                if ok and int(handle.meta.get("ctx_est", 0)) > CHAT_HARD_TOKENS:
                    # 硬阈强铸：马拉松对话没等到静默间隙——就在这个轮尾铸，
                    # 绝不留给 harness auto-compact（§4：那是摘要压缩，信感复发）
                    try:
                        await _consolidate_and_reforge("硬阈强铸",
                                                       handle.meta.get("catalog"))
                    except Exception as e:
                        print(f"[chat_loop] 硬阈强铸失败，session 关掉惰性重起: {e}",
                              file=sys.stderr)
                        await _safe_disconnect(client)
                        client = None
                        ledger = []
                        handle.meta["ledger"] = ledger
                if not ok:
                    print(f"[chat_loop] 轮没收到回复（flags={flags}），"
                          "session 关掉下条重起", file=sys.stderr)
            except asyncio.CancelledError:
                # 轮中被取消（收摊/引擎死外泄）：这轮也要有结局——error+done 让
                # app 的 rescue 观察者能落 error 盒，然后把取消继续往外抛。
                turn.out.put_nowait(sse.sse({"type": "error",
                                             "content": "连接中断了，大模型那边出了点问题。"}))
                turn.out.put_nowait(sse.sse({"type": "done"}))
                raise
            except Exception as e:
                print(f"[chat_loop] 轮异常: {e}", file=sys.stderr)
                turn.out.put_nowait(sse.sse({"type": "error",
                                             "content": "连接中断了，大模型那边出了点问题。"}))
                turn.out.put_nowait(sse.sse({"type": "done"}))
            finally:
                turn.out.put_nowait(None)
                handle.meta.pop("turn_kind", None)   # 门的默认态=放行（巩固轮也走默认）
                if not ok:
                    if turn.on_dead is not None:
                        try:
                            turn.on_dead()
                        except Exception as e:
                            print(f"[chat_loop] on_dead 回调失败: {e}", file=sys.stderr)
                    await _safe_disconnect(client)
                    client = None
                    ledger = []
                    handle.meta["ledger"] = ledger
                handle.wait_user()
    except asyncio.CancelledError:
        # 取消来源纪律（08-30 复盘）：自己人 cancel 必先写 stop_reason；
        # 没写的=SDK 传输层死亡外泄，按引擎异常收摊。
        if not handle.stop_reason:
            handle.stop_reason = "engine-error: cancelled-unexpectedly"
            print("[chat_loop] 计划外 CancelledError（引擎侧取消外泄），按引擎异常收摊",
                  file=sys.stderr)
    except Exception as e:
        print(f"[chat_loop] 引擎异常收摊: {e}", file=sys.stderr)
        handle.stop_reason = handle.stop_reason or f"engine-error: {e}"
    finally:
        why = handle.stop_reason or "unknown-exit"
        print(f"[chat_loop] loop 退出：char={handle.char_id} reason={why}",
              file=sys.stderr)
        await _safe_disconnect(client)


# ---------- 路由入口 ----------

def _ensure_loop(char_id: str) -> session_mgr.LoopHandle:
    h = session_mgr.get(char_id, SCENE)
    if h is not None:
        return h
    got = session_mgr.start(char_id, SCENE, run, watch=None)
    if isinstance(got, dict):                 # 起飞窗口撞了并发请求：拿现成的
        h = session_mgr.get(char_id, SCENE)
        if h is None:
            raise RuntimeError(f"chat loop 起不来: {got.get('error')}")
        return h
    return got


async def stream_turn(*, char_id: str, rid: str, messages: list[dict],
                      injection: str, finalize: Callable,
                      images: Optional[list] = None,
                      file_blocks: Optional[list] = None,
                      catalog: Optional[list] = None) -> AsyncIterator[bytes]:
    """sse.stream_claude 的常驻版替身：塞一轮进该角色的聊天 session，产出 SSE 字节。
    messages=请求带来的完整权威窗口（**含**最后那条新消息，口径同 /chat）。
    必须在事件循环上调（/chat/stream 的 gen 就在循环上）。"""
    hist_all = norm_history(messages)
    if not hist_all:
        raise ValueError("空消息列表")
    turn = Turn(rid=rid, history=hist_all[:-1], new_msg=hist_all[-1],
                injection=injection, finalize=finalize, images=images,
                file_blocks=file_blocks, catalog=catalog)
    handle = _ensure_loop(char_id)
    handle.queue.put_nowait(turn)   # 同一事件循环上，不需要过桥
    while True:
        chunk = await turn.out.get()
        if chunk is None:
            return
        yield chunk
