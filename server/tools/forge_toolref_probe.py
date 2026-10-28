"""重铸能不能「留住取过的用法」探针（不进 CI）。

延迟模式下 ToolSearch 取到的用法在 transcript 里长这样（10-29 从真 session 抄的）：
  assistant tool_use  {name: ToolSearch, input: {query: "select:X", max_results: 1}}
  user      tool_result content=[{"type": "tool_reference", "tool_name": "X"}]
这里用 forge.render 铸一段带这对事件的历史（forge 的 tools 槽目前只收字符串回执，
铸完把那条 tool_result 的 content 换成 tool_reference 列表），resume 后：
  ① get_context_usage 里 X 的 isLoaded 是不是 True（不调模型）
  ② --ask：让模型直接调 X、不许 ToolSearch，看它取不取、调不调得成
对照组：同样的历史但不带这对事件。
"""
import argparse
import asyncio
import dataclasses
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import ClaudeSDKClient  # noqa: E402

import forge  # noqa: E402
import mcp_probe as mp  # noqa: E402

# 目标用带必填参数（名字猜不到）的 probe_echo：没 schema 时模型必然猜错参数名，
# 第一次就传对 zq_token_7＝schema 真的在它眼前（probe_ping 无参，瞎调也能成，测不出）
TARGET = "mcp__probe__probe_echo"


def forge_history(cwd: str, with_ref: bool) -> str:
    t = int(time.time()) - 600
    asst = {"role": "assistant", "ts": t + 5, "text": "好，看了一下，暗号拿到了。"}
    if with_ref:
        asst["tools"] = [{"name": "ToolSearch",
                          "input": {"query": f"select:{TARGET}", "max_results": 1},
                          "result": "__TOOLREF__"}]
    msgs = [{"role": "user", "ts": t, "text": "测试：帮我看一眼探针工具。"}, asst]
    sid = forge.render(msgs, cwd=cwd)
    if with_ref:
        p = forge.transcript_path(cwd, sid)
        lines = p.read_text("utf-8").splitlines()
        out = []
        for ln in lines:
            ev = json.loads(ln)
            c = (ev.get("message") or {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if b.get("type") == "tool_result" and b.get("content") == "__TOOLREF__":
                        b["content"] = [{"type": "tool_reference", "tool_name": TARGET}]
                        ev["toolUseResult"] = {"matches": [TARGET],
                                               "query": f"select:{TARGET}",
                                               "total_deferred_tools": 0}
            out.append(json.dumps(ev, ensure_ascii=False))
        p.write_text("\n".join(out) + "\n", "utf-8")
    return sid


async def one(tag: str, with_ref: bool, ask: bool, char: str, model: str) -> None:
    # realpath：macOS 的 /var 是 /private/var 的软链，CLI 按真路径算 slug 找 transcript
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="mcp_probe_"))
    sid = forge_history(cwd, with_ref)
    opts = mp.make_options(char, deferred=True, cwd=cwd, minimal_system=True)
    opts = dataclasses.replace(opts, resume=sid, model=model,
                               tools=opts.tools + [TARGET],
                               allowed_tools=opts.allowed_tools + [TARGET])
    async with ClaudeSDKClient(opts) as c:
        await mp.poll_status(c)
        u = await c.get_context_usage()
        print(f"[{tag}] resume 后 probe 行：{mp.probe_row(u)}  totalTokens={u.get('totalTokens')}")
        if ask:
            r = await mp.ask(c, f"测试：直接调用 {TARGET}，参数传字符串 hello。不要用 ToolSearch。"
                                "如果调不了就如实说明原因；调成了就把返回原样告诉我。")
            print(f"    ask：调用 {[(x['name'], x['input']) for x in r['tool_uses']]}")
            print(f"    回答 {r['text'][:160]!r}")
            u2 = await c.get_context_usage()
            print(f"    ask 后 probe 行：{mp.probe_row(u2)}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="cass")
    ap.add_argument("--ask", action="store_true")
    ap.add_argument("--models", default="sonnet,opus")
    a = ap.parse_args()
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("环境里有 ANTHROPIC_API_KEY，拒跑")
    for model in a.models.split(","):
        await one(f"{model} 对照：不带 tool_reference", False, a.ask, a.char, model)
        await one(f"{model} 实验：铸进 ToolSearch→tool_reference", True, a.ask, a.char, model)


if __name__ == "__main__":
    asyncio.run(main())
