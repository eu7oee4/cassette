"""轮内插话探针：一轮进行中再 query()，看 CLI 什么时候、以什么形状把它交给模型。

模式：
  text  —— 纯文字长回复中途插话（没有工具边界）
  tool  —— 慢工具（sleep 8s）执行期间插话
  tool2 —— 慢工具期间连插两条
用法：python interject_probe.py <mode>
"""
import asyncio
import sys
import time

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              ResultMessage, SystemMessage, TextBlock, ToolResultBlock,
                              ToolUseBlock, UserMessage, create_sdk_mcp_server, tool)

T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:6.1f}s]", *a, flush=True)


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
                parts.append("TEXT:" + b.text.replace("\n", " ")[:120])
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


async def send(client, text):
    async def gen():
        yield {"type": "user", "message": {"role": "user", "content": text},
               "parent_tool_use_id": None}
    await client.query(gen())
    log(f"<< 已 query：{text!r}")


async def main(mode):
    server = create_sdk_mcp_server("probe", tools=[slow_lookup])
    opts = ClaudeAgentOptions(
        model="claude-haiku-5-5", setting_sources=[], mcp_servers={"probe": server},
        tools=["mcp__probe__slow_lookup"], allowed_tools=["mcp__probe__slow_lookup"],
        system_prompt="你是一个简洁的助手。用户中途补充的话要优先回应。")
    async with ClaudeSDKClient(options=opts) as client:
        if mode == "text":
            first = "写一篇八百字左右的散文，写秋天的一条河。"
            inject = [(4, "停，别数了，只回我一个词：香蕉")]
        else:
            first = "请依次用 slow_lookup 查「苹果」和「橘子」两个词，查完给我一句总结。"
            inject = ([(3, "补充：橘子换成葡萄")] if mode == "tool"
                      else [(3, "补充一：橘子换成葡萄"), (5, "补充二：最后再加查一个西瓜")])
        await send(client, first)

        async def injector():
            for at, text in inject:
                await asyncio.sleep(max(0, at - (time.time() - T0)))
                await send(client, text)
        task = asyncio.create_task(injector())

        results = 0
        deadline = time.time() + 150
        it = client.receive_messages().__aiter__()
        while time.time() < deadline:
            try:
                m = await asyncio.wait_for(it.__anext__(), timeout=deadline - time.time())
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            log(summarize(m))
            if isinstance(m, ResultMessage):
                results += 1
                # 插话都发完、又出了 result 后再等 12s，看有没有第二个 result
                if task.done():
                    try:
                        m2 = await asyncio.wait_for(it.__anext__(), timeout=12)
                        log("(result 之后还有) " + summarize(m2))
                        if isinstance(m2, ResultMessage):
                            results += 1
                        continue
                    except asyncio.TimeoutError:
                        break
        log(f"== 共 {results} 个 result")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
