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
import time

import config
import pipeline
import state_store
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


def _question_events(ev: dict):
    """AskUserQuestion 的 tool_use 一出现就把问答卡推给 app（PLAN_chatui U4）。
    卡的单号=tool_use_id，跟 can_use_tool 那头 questions.ask 登记的一致（回调
    在这个事件之后才 fire，app 拍板太快撞上「还没登记」会拿到 409，重按即可）。
    deadline 是估的（now+超时）：真值在 questions._pending 里，差距毫秒级。
    老 -p 路没挂这个工具，此函数天然空转。"""
    for b in (ev.get("message", {}).get("content") or []):
        if (isinstance(b, dict) and b.get("type") == "tool_use"
                and b.get("name") == "AskUserQuestion"):
            import questions
            yield sse({"type": "question", "id": b.get("id", ""),
                       "questions": (b.get("input") or {}).get("questions") or [],
                       "deadline": int(time.time()) + questions.TIMEOUT_SEC})


_PERMIT_TOOLS = {"Edit", "Write", "NotebookEdit", "Bash"}


def _permit_detail(tool: str, inp: dict) -> tuple[str, str]:
    """(一行摘要, 参数原文) —— 权限卡的正文（PLAN_native §6：Bash 显示命令
    原文，Edit 显示文件和改动）。截断只为运输别撑爆 SSE，拍板要看的头部信息
    都在帽内。"""
    def cap(s, n: int) -> str:
        s = str(s or "")
        return s if len(s) <= n else s[:n] + "\n…（截断）"
    inp = inp or {}
    if tool == "Bash":
        cmd = str(inp.get("command") or "")
        first = cmd.splitlines()[0] if cmd else ""
        return first[:120], cap(cmd, 2000)
    if tool == "Edit":
        fp = str(inp.get("file_path") or "")
        return fp, (fp + "\n── 删：\n" + cap(inp.get("old_string"), 600)
                    + "\n── 换成：\n" + cap(inp.get("new_string"), 600))
    if tool in ("Write", "NotebookEdit"):
        fp = str(inp.get("file_path") or inp.get("notebook_path") or "")
        body = inp.get("content") or inp.get("new_source")
        return fp, fp + "\n" + cap(body, 1200)
    return "", cap(json.dumps(inp, ensure_ascii=False), 800)


def _permit_events(ev: dict):
    """写类 tool_use 一出现就把权限卡推给 app（PLAN_native §6 / chatui U4）。
    单号=tool_use_id，跟 can_use_tool 那头 permits.ask 登记的一致。
    ⚠️ 这儿在权限判定**之前**：被 PreToolUse 路径闸拒掉的调用到不了 permits，
    会推出一张「幽灵卡」——app 侧靠 pending 轮询收走（没超时又不在册＝作废），
    拍板撞上 409 也有声。路径闸拒是罕见路，实时性换这个代价划算。
    老 -p 路没挂写类工具，此函数天然空转。"""
    for b in (ev.get("message", {}).get("content") or []):
        if (isinstance(b, dict) and b.get("type") == "tool_use"
                and b.get("name") in _PERMIT_TOOLS):
            import permits
            summary, detail = _permit_detail(b["name"], b.get("input") or {})
            yield sse({"type": "permit", "id": b.get("id", ""),
                       "tool": b["name"], "summary": summary, "detail": detail,
                       "deadline": int(time.time()) + permits.CHAT_TIMEOUT_SEC})


def _memory_events(item: dict):
    """一条定了案的 stored → 要发给 app 的 memory 事件（0 或 1 条）。
    codemode/gamemode 是借 stored 走的控制信号（TA 自切 code 模式/游戏会话），不是
    记忆产物——发成 memory 事件的话 app 会渲染出一条莫名其妙的灰字。
    browse 不逐条发（连点几个页面会刷屏）：finalize 聚合成一条随 done 走。
    gametask **要发**：派引擎跑日常值得一条可核对的灰字（app 按 tool 渲染文案）。"""
    if item["tool"] in ("codemode", "gamemode", "browse"):
        return
    yield sse({"type": "memory", "tool": item["tool"], "text": item["text"],
               "name": item.get("name", ""),   # 裸工具名（§7.2 拍板：小字文案=它）
               "ok": item.get("ok", True), "error": item.get("error", "")})


async def translate_events(events, finalize):
    """纯翻译：事件流 → SSE 字节块。不碰子进程，便于单测。
    events：async 迭代器，逐个产出已解析的事件 dict（含 __idle_timeout__ 哨兵）。
    finalize：拿全文 (reply, stored) 组出 done 载荷（dict）的回调。"""
    mf = MarkerStreamFilter()
    collector = pipeline.StoredCollector()   # tool_use 只是意图；灰字等 tool_result 定案再发
    raw_segments: list[str] = []   # 工具调用切开的正文段（原始未滤标记；最后一段以 result 为准）
    cur_raw = ""
    sealed = ""     # 已定稿的正文段拼好的样子；每个 delta 只要接上 cur_raw 就是"到此为止说过的话"
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
                        sealed = "\n\n".join(s.strip() for s in raw_segments if s.strip()) + "\n\n"
                        yield sse({"type": "text_break"})
                    cur_raw = ""
                    mf = MarkerStreamFilter()
            elif st == "content_block_delta":
                d = se.get("delta", {})
                if d.get("type") == "text_delta":
                    txt = d.get("text", "")
                    cur_raw += txt
                    # 说到哪儿了同步给自切 code 那条路：它是轮跑到一半触发的，recent_window
                    # 那时候还没有这轮的回复（见 state_store.set_live_reply 上面那段）。
                    state_store.set_live_reply(sealed + cur_raw)
                    emit = mf.feed(txt)
                    if emit:
                        yield sse({"type": "text", "content": emit})
        elif t == "assistant":
            # 工具调用先登记（去重、抓产物），**灰字这时候还不发**——这里只知道他想干什么，
            # 干成没干成要等下面的 tool_result。先发的话失败的调用也会亮一条「记住了一件事」。
            collector.on_assistant(ev)
            for chunk in _question_events(ev):
                yield chunk
            for chunk in _permit_events(ev):
                yield chunk
        elif t == "user":
            # 工具返回了 → 定案，成功/失败各自往下游发一条 memory 灰字。
            for item in collector.on_user(ev):
                for chunk in _memory_events(item):
                    yield chunk
        elif t == "result":
            if ev.get("is_error"):
                is_error = True
                logerr(f"/chat/stream result 报错: {ev.get('subtype')}")
                break
            result_text = ev.get("result")

    # ---- 收尾 ----
    # 还没等到返回的工具（流断在半路）：定案成失败，该说的还是要说。
    for item in collector.finish():
        for chunk in _memory_events(item):
            yield chunk

    if is_error or not result_text or not result_text.strip():
        # 空/错：用 SSE 格式收尾（响应头已发出，不能再抛 JSON），不存空回复。
        yield sse({"type": "error", "content": "大模型好像有点神游了，这次没能说完。"})
        yield sse({"type": "done"})
        return

    # 权威回复 = 全部正文段拼接（工具前说的话也是话，进历史不能丢）。
    full_reply = "\n\n".join([s.strip() for s in raw_segments if s.strip()] + [result_text.strip()])
    payload = finalize(full_reply, collector.items)
    payload["type"] = "done"
    yield sse(payload)


async def _watch_bp(events):
    """透传事件流，顺手盯住「缓存断点超限」那个 400——一发现就关掉断点，下一条消息起自动
    降级（只是变慢变贵，不再报错）。

    流式这条路**故意不原地重试**：响应头早发出去了，重起子进程会把已经上屏的字打乱，
    为一个只在 CLI 升级那一刻出现一次的故障冒这个险不划算。代价是眠眠会看到一次
    「神游了」、重发一遍就好；日志里有明确的一行说清是什么事。
    非流式那条路（pipeline._call_claude）响应还没开始，照旧原地重试，上层无感。"""
    async for ev in events:
        if (ev.get("type") == "result" and ev.get("api_error_status") == 400
                and pipeline.bp_limit_hit(ev.get("result"))):
            pipeline.disable_cache_bp("流式")
        yield ev


async def stream_claude(prompt: str, translate, images: list | None = None,
                        file_blocks: list | None = None, char_id: str | None = None):
    """通用流式：起 claude 子进程 → 把 stream-json 事件交给 translate(events) 翻成 SSE 字节块。
    安全约定同 pipeline.call_claude：base_claude_args + 删 ANTHROPIC_API_KEY。
    stdin 一律走 stream-json 的 user 消息（原来只有带图才走）：缓存断点得能拆 content
    block 才打得上，纯文本 stdin 给不了这个。模型看到的字节不变，只是投递方式变了。"""
    args = pipeline.base_claude_args(char_id=char_id) + \
        ["--input-format", "stream-json", "--output-format", "stream-json",
         "--verbose", "--include-partial-messages"]
    stdin_payload = pipeline.stdin_payload(prompt, images, file_blocks)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=pipeline._subprocess_env(),
        cwd=pipeline.neutral_cwd(),
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
        async for chunk in translate(_watch_bp(read_stream_events(proc))):
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
