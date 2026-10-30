"""轮内插话探针：一轮进行中再 query()，看 CLI 什么时候、以什么形状把它交给模型。

模式：
  text   —— 纯文字长回复中途插话（没有工具边界）
  tool   —— 慢工具（sleep 8s）执行期间插话
  tool2  —— 慢工具期间连插两条
  verify —— (P0-a) tool 模式 + 让模型把中途收到的那条新消息连同包装一字不差复述
  image  —— (P0-b) 慢工具期间插一条带 image 块（纯红 64×64 PNG）的 user 消息
  toollist —— 同 tool，但插话 content 是 [{"type":"text",...}] 列表（看渲染 role 是否随形状变）
用法：python interject_probe.py <mode> [--model haiku|opus|...] [--hooks] [--watch] [--logdir DIR]
  --hooks  (P0-c) 挂 UserPromptSubmit hook，计数并记 hook_input 原文
  --watch  (P0-d) 另起 task 每 20ms 读 transcript 尾部，记各关键行首次落盘的单调时刻
  --logdir 跑完把 transcript 复制一份过去（文件名 <mode>_<model>_<sid>.jsonl）
"""
import argparse
import asyncio
import base64
import json
import os
import shutil
import struct
import time
import zlib
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              HookMatcher, ResultMessage, SystemMessage, TextBlock,
                              ToolResultBlock, ToolUseBlock, UserMessage,
                              create_sdk_mcp_server, tool)

T0 = time.time()
M0 = time.monotonic()
FULL_TEXT = False          # verify/image 模式下完整打印模型文字
HOOK_EVENTS: list = []     # (mono, wall, hook_input)
MARKS: dict = {}           # P0-d：流里 / transcript 里关键事件首次出现的单调时刻


def mono():
    return time.monotonic() - M0


def log(*a):
    print(f"[{mono():7.3f}s]", *a, flush=True)


def mark(key, extra=""):
    if key not in MARKS:
        MARKS[key] = mono()
        log(f"  ## MARK {key} @ {MARKS[key]:.3f}s {extra}")


@tool("slow_lookup", "查一个词的资料，要花大约 8 秒", {"word": str})
async def slow_lookup(args):
    log(f"  >> 工具开始执行 word={args['word']}")
    await asyncio.sleep(8)
    log("  >> 工具执行完")
    return {"content": [{"type": "text", "text": f"{args['word']}：资料若干条（占位）"}]}


def summarize(m):
    if isinstance(m, AssistantMessage):
        parts = []
        for b in m.content:
            if isinstance(b, TextBlock):
                t = b.text if FULL_TEXT else b.text.replace("\n", " ")[:120]
                parts.append("TEXT:" + t)
            elif isinstance(b, ToolUseBlock):
                parts.append(f"TOOL_USE:{b.name}{b.input}")
            else:
                parts.append(type(b).__name__)
        return "Assistant " + " | ".join(parts)
    if isinstance(m, UserMessage):
        c = m.content
        if isinstance(c, str):
            return f"User(str) {c[:120]!r}"
        parts = []
        for b in c:
            if isinstance(b, ToolResultBlock):
                parts.append("TOOL_RESULT")
            elif isinstance(b, TextBlock):
                parts.append("TEXT:" + b.text[:120])
            else:
                parts.append(type(b).__name__ + ":" + str(getattr(b, "text", b))[:120])
        extra = {k: getattr(m, k) for k in ("uuid", "parent_tool_use_id") if getattr(m, k, None)}
        return f"User {parts} {extra}"
    if isinstance(m, ResultMessage):
        return (f"RESULT subtype={m.subtype} is_error={m.is_error} turns={m.num_turns} "
                f"result={(m.result or '')[:160]!r}")
    if isinstance(m, SystemMessage):
        sid = (m.data or {}).get("session_id") if m.subtype == "init" else ""
        return f"System {m.subtype} {sid}"
    return type(m).__name__


async def send(client, content):
    """content: str 或 content-block 列表（形状照 chat_loop._payload）。"""
    async def gen():
        yield {"type": "user", "message": {"role": "user", "content": content},
               "parent_tool_use_id": None}
    await client.query(gen())
    shown = content if isinstance(content, str) else [
        (b["type"], b.get("text", "")[:60]) for b in content]
    log(f"<< 已 query：{shown!r}")
    mark("query_sent#%d" % (sum(1 for k in MARKS if k.startswith("query_sent")) + 1))


def red_png(size=64):
    """不依赖 PIL，手写一张纯红 RGB PNG。"""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# ---------- P0-c：hook ----------
async def on_user_prompt_submit(hook_input, tool_use_id, ctx):
    HOOK_EVENTS.append((mono(), time.time(), hook_input))
    log(f"  ## HOOK UserPromptSubmit #{len(HOOK_EVENTS)} prompt={hook_input.get('prompt')!r}")
    return {}


# ---------- P0-d：transcript 尾部监视 ----------
def transcript_path(sid):
    proj = os.getcwd().replace("/", "-")
    return Path.home() / ".claude" / "projects" / proj / f"{sid}.jsonl"


def classify(o):
    t = o.get("type")
    if t == "attachment":
        at = (o.get("attachment") or {}).get("type")
        if at == "queued_command":
            return "T:queued_command", f"parent={o.get('parentUuid')} prompt={str((o['attachment']).get('prompt'))[:60]!r}"
        return None
    if t == "queue-operation":
        op = o.get("operation")
        if op == "enqueue":
            return "T:enqueue", repr(str(o.get("content"))[:60])
        if op == "dequeue":
            return "T:dequeue", ""
        if op == "remove":
            return "T:remove:" + str(o.get("reason")), repr(str(o.get("content"))[:60])
        return None
    if t == "api-request-blob":
        m = o.get("message") or {}
        role = m.get("role")
        c = m.get("content")
        txt = ""
        has_img = False
        if isinstance(c, list):
            for b in c:
                if b.get("type") == "text":
                    txt += b.get("text", "")
                if b.get("type") == "image":
                    has_img = True
        elif isinstance(c, str):
            txt = c
        if role == "system" and "new message while you were working" in txt:
            return "T:blob_system_midturn", f"hash={o.get('hash','')[:8]}"
        if has_img:
            return f"T:blob_{role}_with_image", f"hash={o.get('hash','')[:8]}"
        if "new message while you were working" in txt:
            return f"T:blob_{role}_midturn", f"hash={o.get('hash','')[:8]}"
        return None
    if t == "api-request":
        return "T:api-request", f"ts={o.get('timestamp')} tail={[h[:8] for h in o.get('tail', [])]}"
    if t == "user":
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list) and any(b.get("type") == "tool_result" for b in c):
            return "T:user_tool_result", f"uuid={o.get('uuid')}"
        return "T:user_prompt", f"uuid={o.get('uuid')} promptId={o.get('promptId')}"
    if t == "assistant":
        return "T:assistant", f"uuid={o.get('uuid')}"
    return None


async def watch_transcript(path: Path, stop: asyncio.Event, out: list):
    """每 20ms 读一次尾部；每个 key 首次出现记单调时刻，重复 key 带序号。"""
    off = 0
    buf = b""
    counts: dict = {}
    while not stop.is_set():
        try:
            with open(path, "rb") as f:
                f.seek(off)
                data = f.read()
        except FileNotFoundError:
            data = b""
        if data:
            off += len(data)
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                r = classify(o)
                if not r:
                    continue
                key, extra = r
                counts[key] = counts.get(key, 0) + 1
                k2 = f"{key}#{counts[key]}"
                out.append((mono(), time.time(), k2, extra))
                mark(k2, extra)
        await asyncio.sleep(0.02)


async def main(mode, model, use_hooks, use_watch, logdir):
    global FULL_TEXT
    FULL_TEXT = mode in ("verify", "image")
    server = create_sdk_mcp_server("probe", tools=[slow_lookup])
    kw = {}
    if use_hooks:
        kw["hooks"] = {"UserPromptSubmit": [HookMatcher(hooks=[on_user_prompt_submit])]}
    opts = ClaudeAgentOptions(
        model=model, setting_sources=[], mcp_servers={"probe": server},
        tools=["mcp__probe__slow_lookup"], allowed_tools=["mcp__probe__slow_lookup"],
        system_prompt="你是一个简洁的助手。用户中途补充的话要优先回应。", **kw)
    sid = None
    watch_rows: list = []
    stop = asyncio.Event()
    watcher = None
    async with ClaudeSDKClient(options=opts) as client:
        if mode == "text":
            first = "写一篇八百字左右的散文，写秋天的一条河。"
            inject = [(4, "停，别数了，只回我一个词：香蕉")]
        elif mode == "verify":
            first = ("请依次用 slow_lookup 查「苹果」和「橘子」两个词。查完之后先做一件事：如果你在查的过程中"
                     "收到过任何新消息，请把**包含那条新消息的整段文字从头到尾一字不差地复述出来**（包括包裹它的"
                     "所有说明文字、标签、换行），然后分别回答：①它在你上下文里是以什么 role 出现的（user / "
                     "system / 工具结果 / 其它）；②它是谁发的；③它有没有带时间戳或日期；④它是不是和别的内容"
                     "（比如工具列表）拼在同一段里。最后再给一句总结。")
            inject = [(3, "补充：橘子换成葡萄")]
        elif mode == "image":
            first = "请依次用 slow_lookup 查「苹果」和「橘子」两个词，查完给我一句总结。"
            img_b64 = base64.b64encode(red_png()).decode()
            inject = [(3, [{"type": "text", "text": "新消息里这张图是什么颜色？"},
                           {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": img_b64}}])]
        elif mode == "toollist":
            # 同 tool，但插话的 content 是 block 列表（不带图）——看渲染 role 跟 str 形状一样不一样
            first = "请依次用 slow_lookup 查「苹果」和「橘子」两个词，查完给我一句总结。"
            inject = [(3, [{"type": "text", "text": "补充：橘子换成葡萄"}])]
        else:
            first = "请依次用 slow_lookup 查「苹果」和「橘子」两个词，查完给我一句总结。"
            inject = ([(3, "补充：橘子换成葡萄")] if mode == "tool"
                      else [(3, "补充一：橘子换成葡萄"), (5, "补充二：最后再加查一个西瓜")])
        await send(client, first)
        base = time.time()   # 插话时刻从首条 query 送出算起（client 启动 3s+ 时按 T0 算会撞到首条之前）

        async def injector():
            for at, text in inject:
                await asyncio.sleep(max(0, at - (time.time() - base)))
                await send(client, text)
        task = asyncio.create_task(injector())

        results = 0
        n_tr = n_asst = 0
        deadline = time.time() + 150
        it = client.receive_messages().__aiter__()
        while time.time() < deadline:
            try:
                m = await asyncio.wait_for(it.__anext__(), timeout=deadline - time.time())
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            log(summarize(m))
            if isinstance(m, SystemMessage) and m.subtype == "init" and sid is None:
                sid = (m.data or {}).get("session_id")
                log(f"  transcript: {transcript_path(sid)}")
                if use_watch:
                    watcher = asyncio.create_task(watch_transcript(transcript_path(sid), stop, watch_rows))
            if isinstance(m, SystemMessage) and m.subtype == "init":
                mark("S:init#%d" % (sum(1 for k in MARKS if k.startswith("S:init")) + 1))
            if isinstance(m, UserMessage) and not isinstance(m.content, str) and \
                    any(isinstance(b, ToolResultBlock) for b in m.content):
                n_tr += 1
                mark(f"S:tool_result#{n_tr}", f"uuid={getattr(m, 'uuid', None)}")
            if isinstance(m, AssistantMessage):
                n_asst += 1
                mark(f"S:assistant#{n_asst}")
            if isinstance(m, ResultMessage):
                results += 1
                mark(f"S:result#{results}")
                # 插话都发完、又出了 result 后再等 12s，看有没有第二个 result
                if task.done():
                    try:
                        m2 = await asyncio.wait_for(it.__anext__(), timeout=12)
                        log("(result 之后还有) " + summarize(m2))
                        if isinstance(m2, SystemMessage) and m2.subtype == "init":
                            mark("S:init#%d" % (sum(1 for k in MARKS if k.startswith("S:init")) + 1))
                        if isinstance(m2, ResultMessage):
                            results += 1
                            mark(f"S:result#{results}")
                        continue
                    except asyncio.TimeoutError:
                        break
        log(f"== 共 {results} 个 result")
        await asyncio.sleep(0.5)
        stop.set()
        if watcher:
            await watcher

    # ---- 收尾：打印 hook / marks / 复制 transcript ----
    if use_hooks:
        log(f"== UserPromptSubmit hook 共触发 {len(HOOK_EVENTS)} 次")
        for i, (mo, wall, hi) in enumerate(HOOK_EVENTS, 1):
            log(f"   hook#{i} @ {mo:.3f}s input={json.dumps(hi, ensure_ascii=False)[:600]}")
    if MARKS:
        log("== MARKS（单调秒，按时间排）")
        for k, v in sorted(MARKS.items(), key=lambda kv: kv[1]):
            print(f"   {v:8.3f}  {k}", flush=True)
    if sid and logdir:
        src = transcript_path(sid)
        Path(logdir).mkdir(parents=True, exist_ok=True)
        dst = Path(logdir) / f"{mode}_{model.replace('/', '_')}_{sid}.jsonl"
        await asyncio.sleep(1.0)
        if src.exists():
            shutil.copy(src, dst)
            log(f"== transcript 已复制到 {dst}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["text", "tool", "tool2", "verify", "image", "toollist"])
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--hooks", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--logdir", default=None)
    a = ap.parse_args()
    asyncio.run(main(a.mode, a.model, a.hooks, a.watch, a.logdir))
