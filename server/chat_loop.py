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

SCENE = "chat"
LEDGER_DIR = state_store.STATE_DIR / "chat_sessions"
# 浅层错位容忍：请求发出时上一轮回复还没落进 app 历史（连发竞态）/wake 气泡还没
# 被 app 拉走——账尾多出的**纯 assistant** 条目不算脏。超过这个深度按脏处理。
STALE_TOLERANCE = 8


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
    （done 正常走完 ∣ error+done），补投/error 盒仍归 app.py 的 rescue 观察者。"""
    rid: str
    history: list[dict]                  # 权威窗口（不含新消息），已 norm
    new_msg: dict                        # {"role":"user","text","ts"}（入账用原文）
    injection: str                       # 实际注入的包装文本（不进账）
    finalize: Callable[[str, list], dict]
    images: Optional[list] = None
    file_blocks: Optional[list] = None
    catalog: Optional[list] = None       # 表情目录：重铸/起 session 时进系统提示
    out: asyncio.Queue = field(default_factory=asyncio.Queue)


# ---------- options ----------

def _merge_mcp_file(servers: dict, cfg_path: str) -> None:
    try:
        data = json.loads(open(cfg_path, encoding="utf-8").read())
        servers.update(data.get("mcpServers", {}))
    except Exception as e:
        print(f"[chat_loop] MCP 配置解析失败（跳过 {cfg_path}）: {e}", file=sys.stderr)


def build_options(char_id: str, catalog: Optional[list] = None) -> ClaudeAgentOptions:
    """口径对齐 pipeline.base_claude_args（chat 路）：人设+菜单+叮嘱进系统提示，
    Ombre http 直传 dict，插件/宠物/skills 的 mcp.json 解析合并，白名单+strict。
    与 -p 的差别只有结构：稳定段从每轮 prompt 挪进 session 系统提示（付一次）。"""
    import pipeline
    if os.environ.get("ANTHROPIC_API_KEY"):
        # -p 那条路每次 pop 掉 key；SDK 子进程继承我们的 env，没法逐调用摘——
        # 有 key 就拒开，绝不静默改成 API 计费（订阅纪律）。
        raise RuntimeError("环境里有 ANTHROPIC_API_KEY：SDK 聊天路会走 API 计费，拒开")

    parts = [pipeline.rendered_persona(char_id).read_text("utf-8")]
    mb = pipeline.tool_menu_block("chat", char_id)
    if mb:
        parts.append(mb)
    parts += [pipeline.pronoun_hint(), pipeline._chat_next_hint()]
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
    )


# ---------- 注入正文（易变段；稳定段在系统提示里） ----------

def build_injection(messages, char_id: Optional[str],
                    extra_hints: Optional[list[str]] = None) -> str:
    """一轮的包装文本：叮嘱+待办+时间感+新消息。口径抄 build_prompt 的易变段——
    历史不在这儿（历史活在 transcript 里，这正是整个迁移的意义）。"""
    import pipeline
    last = messages[-1]
    lines = [h for h in (extra_hints or []) if h]
    lines.append(pipeline.one_turn_hint("chat"))
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
            _note_usage(handle.char_id, msg)
            if msg.is_error:
                flags["error_subtype"] = msg.subtype or "?"
            yield {"type": "result", "result": msg.result,
                   "is_error": msg.is_error, "subtype": msg.subtype,
                   "api_error_status": msg.api_error_status}
            return


def _note_usage(char_id: str, msg: ResultMessage) -> None:
    """usage 落账（PLAN_sdk §10 S2 设计稿二：手机端 usage 面板的数据源）。
    append-only jsonl 按天分文件；写失败只记日志，绝不影响聊天轮。"""
    try:
        d = state_store.STATE_DIR / "usage"
        d.mkdir(exist_ok=True)
        rec = {"ts": int(time.time()), "char": char_id, "scene": SCENE,
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
    opts_factory = options_factory or build_options
    client = None
    sid: Optional[str] = None
    ledger: list[dict] = []
    handle.meta["ledger"] = ledger    # 测试/观测窗口

    async def _open_session(turn: Turn):
        nonlocal client, sid, ledger
        await _safe_disconnect(client)
        client = None
        options = opts_factory(handle.char_id, turn.catalog)
        if turn.history:
            sid = forge.render(turn.history, cwd=str(options.cwd),
                               model=config.MODEL)
            options = copy.copy(options)
            options.resume = sid
        else:
            sid = None
        c = factory(options)
        await c.connect()
        ledger = [{"r": m["role"], "h": _h(m["text"]), "ts": m.get("ts")}
                  for m in turn.history]
        handle.meta["ledger"] = ledger
        client = c

    try:
        while True:
            turn: Turn = await handle.queue.get()
            handle.touch()
            ok = False
            try:
                verdict = divergence(ledger, turn.history) if client else "dirty"
                if verdict == "dirty":
                    if client:
                        print(f"[chat_loop] 判脏（char={handle.char_id}），重铸",
                              file=sys.stderr)
                    await _open_session(turn)
                # ---- 注入这一轮 ----
                content: list[dict] = [{"type": "text", "text": turn.injection}]
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
                    captured["reply"] = payload.get("reply") or reply
                    return payload

                async for chunk in sse.translate_events(
                        _turn_events(client, handle, flags), _fin):
                    turn.out.put_nowait(chunk)

                if captured.get("reply"):
                    ledger.append({"r": "user", "h": _h(turn.new_msg["text"]),
                                   "ts": turn.new_msg.get("ts")})
                    ledger.append({"r": "assistant", "h": _h(captured["reply"]),
                                   "ts": int(time.time())})
                    _persist_ledger(handle.char_id, sid, ledger)
                    ok = True
                else:
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
                if not ok:
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
