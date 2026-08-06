"""
SSE 流式：把 claude CLI 的 stream-json 事件流翻成前端统一 SSE 协议。

事件类型：
  text（正文片段）/ text_break（当前气泡定稿保留、下一段另起气泡）/
  memory（工具产物灰字，后续模块用）/ ping（心跳）/ error / done（附完整 ChatResponse）。

text_break：工具调用会把正文切成多段，CLI 的 result 只含最后一段。"先说一句再去干活"
是正经对话不能删——每段一个气泡（break=定稿），权威回复=全段拼接（done 时 finalize
收到完整正文，历史不丢话）。

全链路三道流式翻译：Anthropic SSE → claude stream-json(stdout) → 这里转回 SSE → app。
任何一道退化成整段转发，首字延迟就从亚秒变十几秒。
"""
import asyncio
import json

import config
import pipeline
from notify import logerr


def sse(obj: dict) -> bytes:
    """拼一条 SSE 事件（UTF-8 bytes）。"""
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


class MarkerStreamFilter:
    """把逐字增量里的内联标记 [[...]] 挡在气泡外。标记可能被拆到两段增量，
    未闭合就扣住等下一段。权威回复由 done 时 finalize 兜底剥，这里只求别露给用户。"""
    def __init__(self):
        self.buf = ""

    def feed(self, text: str) -> str:
        """喂一段增量，返回可安全上屏的文字。"""
        self.buf += text
        out: list[str] = []
        while True:
            idx = self.buf.find("[[")
            if idx == -1:
                if self.buf.endswith("["):     # 留住可能拼成 "[[" 的单个 "["
                    out.append(self.buf[:-1]); self.buf = "["
                else:
                    out.append(self.buf); self.buf = ""
                break
            out.append(self.buf[:idx])
            rest = self.buf[idx:]
            close = rest.find("]]")
            if close == -1:
                # 标记未闭合：扣住等下一段。但设上限——模型写了 [[ 之后一直不闭合
                # （跑偏/正文里合法出现双方括号）会把后续全部输出冻在缓冲里，打字看着停住。
                # 合法标记都很短，超过 120 字符就断定不是标记，整段放行。
                if len(rest) > 120:
                    out.append(rest)
                    self.buf = ""
                else:
                    self.buf = rest
                break
            self.buf = rest[close + 2:]        # 整段吞掉
        return "".join(out)


async def read_stream_events(proc):
    """逐行读子进程 stdout，产出解析好的 stream-json 事件 dict。
    空闲超过超时视为卡死 → 产出 __idle_timeout__ 哨兵后收尾（事件会重置计时=卡死检测）。"""
    while True:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(),
                                          timeout=config.CLAUDE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            logerr("/chat/stream 空闲超时")
            yield {"type": "__idle_timeout__"}
            return
        except ValueError as e:
            # 单行超出 StreamReader limit：limit 已调到 64MB，还能爆说明极端异常，按卡死收尾。
            logerr(f"/chat/stream 单行超限: {e}")
            yield {"type": "__idle_timeout__"}
            return
        if not line:
            return
        try:
            yield json.loads(line.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue


async def translate_events(events, finalize):
    """纯翻译：事件流 → SSE 字节块。不碰子进程，便于单测。
    events：async 迭代器，逐个产出已解析的事件 dict（含 __idle_timeout__ 哨兵）。
    finalize：拿全文 (reply, stored) 组出 done 载荷（dict）的回调。"""
    mf = MarkerStreamFilter()
    stored: list[dict] = []
    raw_segments: list[str] = []   # 工具调用切开的正文段（原始未滤标记；最后一段以 result 为准）
    cur_raw = ""
    result_text = None
    is_error = False

    async for ev in events:
        t = ev.get("type")
        if t == "__idle_timeout__":
            is_error = True
            break
        if t == "stream_event":
            se = ev.get("event", {})
            st = se.get("type")
            if st == "content_block_start":
                if se.get("content_block", {}).get("type") == "text":
                    # 新 text 块 = 一段说完去用了工具又回来 → 当前气泡定稿，另起一个。
                    if cur_raw.strip():
                        raw_segments.append(cur_raw)
                        yield sse({"type": "text_break"})
                    cur_raw = ""
                    mf = MarkerStreamFilter()
            elif st == "content_block_delta":
                d = se.get("delta", {})
                if d.get("type") == "text_delta":
                    txt = d.get("text", "")
                    cur_raw += txt
                    emit = mf.feed(txt)
                    if emit:
                        yield sse({"type": "text", "content": emit})
        elif t == "assistant":
            # 工具调用产物（记忆/网页等）抓进 stored + 往下游发 memory 灰字事件（气泡间可见）。
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") != "tool_use":
                    continue
                s = pipeline._stored_from_tool_use(b.get("name", ""), b.get("input", {}) or {})
                if s:
                    stored.append(s)
                    yield sse({"type": "memory", "tool": s["tool"], "text": s["text"]})
        elif t == "result":
            if ev.get("is_error"):
                is_error = True
                logerr(f"/chat/stream result 报错: {ev.get('subtype')}")
                break
            result_text = ev.get("result")

    # ---- 收尾 ----
    if is_error or not result_text or not result_text.strip():
        # 空/错：用 SSE 格式收尾（响应头已发出，不能再抛 JSON），不存空回复。
        yield sse({"type": "error", "content": "大模型好像有点神游了，这次没能说完。"})
        yield sse({"type": "done"})
        return

    # 权威回复 = 全部正文段拼接（工具前说的话也是话，进历史不能丢）。
    full_reply = "\n\n".join([s.strip() for s in raw_segments if s.strip()] + [result_text.strip()])
    payload = finalize(full_reply, stored)
    payload["type"] = "done"
    yield sse(payload)


async def stream_claude(prompt: str, translate, images: list | None = None,
                        file_blocks: list | None = None):
    """通用流式：起 claude 子进程 → 把 stream-json 事件交给 translate(events) 翻成 SSE 字节块。
    安全约定同 pipeline.call_claude：base_claude_args + 删 ANTHROPIC_API_KEY。
    带图/文件 → stdin 换成 stream-json 的多模态 user 消息（模型真看到），其余不变。"""
    args = pipeline.base_claude_args() + \
        ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    if images or file_blocks:
        args += ["--input-format", "stream-json"]
        stdin_payload = pipeline.multimodal_stdin(prompt, images or [], file_blocks)
    else:
        stdin_payload = prompt

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=pipeline._subprocess_env(),
        # stream-json 一个事件一整行：大 tool_result 会撑爆 StreamReader 默认 64KB 行限。
        limit=64 * 1024 * 1024,
    )
    # 后台把 stderr 抽干，别让管道写满把子进程卡住。
    async def _drain_stderr():
        try:
            async for _ in proc.stderr:
                pass
        except Exception:
            pass
    stderr_task = asyncio.create_task(_drain_stderr())

    # prompt 走 stdin，写完关掉（claude -p 读到 EOF 才开始）。
    proc.stdin.write(stdin_payload.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    try:
        async for chunk in translate(read_stream_events(proc)):
            yield chunk
    finally:
        stderr_task.cancel()
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await proc.wait()
        except Exception:
            pass
