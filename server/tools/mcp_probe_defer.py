"""按 server / 按工具单独控制延迟的探针（mcp_probe.py 的续篇，不进 CI）。

CLI 2.1.310 的 MCP 配置认 `alwaysLoad`（strings 里的原文：When true, all tools from this
server are always included in the prompt and never deferred behind tool search, except a
tool the server itself lists with _meta anthropic/alwaysLoad set to false ... When false,
all tools from this server are deferred behind tool search. Default: tools are deferred
when tool search is enabled.）；单工具认 _meta["anthropic/alwaysLoad"]。

两组（都不调模型，读 get_context_usage）：
  D1  ENABLE_TOOL_SEARCH=true；ombre-brain(http)/basics(stdio)/game(进程内 sdk) 设
      alwaysLoad=true；probe(stdio) 不设，但它的 probe_pinned 带单工具 _meta=true。
  D2  不设 ENABLE_TOOL_SEARCH（ToolSearch 照挂）；只给 galatea 设 alwaysLoad=false
      ——看能不能「默认全量、只延迟一个 server」。
--ask：D1 里用 sonnet 问一次，直接调 basics 的 now（不取用法）能不能成。
"""
import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dataclasses  # noqa: E402

from claude_agent_sdk import ClaudeSDKClient  # noqa: E402

import mcp_probe as mp  # noqa: E402
import pipeline  # noqa: E402


def with_flags(opts, flags: dict, extra_tools=(), env_search=None):
    servers = {}
    for name, cfg in opts.mcp_servers.items():
        cfg = dict(cfg)
        if name in flags:
            cfg["alwaysLoad"] = flags[name]
        servers[name] = cfg
    tools = list(opts.tools) + [t for t in extra_tools if t not in opts.tools]
    env = dict(opts.env or {})
    if env_search is None:
        env.pop("ENABLE_TOOL_SEARCH", None)
    else:
        env["ENABLE_TOOL_SEARCH"] = env_search
    return dataclasses.replace(opts, mcp_servers=servers, tools=tools,
                               allowed_tools=tools, env=env)


def report(tag: str, u: dict) -> dict:
    by: dict[str, dict] = {}
    for t in u.get("mcpTools") or []:
        s = by.setdefault(t.get("serverName", "?"), {"loaded": [], "deferred": [], "tok_l": 0, "tok_d": 0})
        k = "loaded" if t.get("isLoaded") else "deferred"
        s[k].append(t["name"].rsplit("__", 1)[-1])
        s["tok_l" if t.get("isLoaded") else "tok_d"] += int(t.get("tokens") or 0)
    print(f"\n[{tag}] totalTokens={u.get('totalTokens')}")
    for c in u.get("categories", []):
        print(f"    {c['name']:<26} {c['tokens']:>7}{'  (deferred)' if c.get('isDeferred') else ''}")
    for srv, v in sorted(by.items()):
        nl, nd = len(v["loaded"]), len(v["deferred"])
        mixed = f"  常驻={v['loaded']}" if nl and nd else ""
        print(f"    {srv:<14} 常驻 {nl:>3} ({v['tok_l']:>5} tok)  延迟 {nd:>3} ({v['tok_d']:>5} tok){mixed}")
    return by


async def run(tag, opts, ask=False):
    async with ClaudeSDKClient(opts) as c:
        await mp.poll_status(c)
        u = await c.get_context_usage()
        report(tag, u)
        if ask:
            r = await mp.ask(c, "这是一次测试。直接调用 mcp__basics__now（不要用 ToolSearch），"
                                "然后用一句话告诉我它返回的时间。")
            print(f"    ask：调用 {[x['name'] for x in r['tool_uses']]}  回答 {r['text'][:80]!r}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="cass")
    ap.add_argument("--ask", action="store_true")
    a = ap.parse_args()
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("环境里有 ANTHROPIC_API_KEY，拒跑")
    cwd = tempfile.mkdtemp(prefix="mcp_probe_")
    base = mp.make_options(a.char, deferred=True, cwd=cwd, minimal_system=True)
    extra = ["mcp__probe__probe_pinned"]
    d1 = with_flags(base, {"ombre-brain": True, "basics": True, "game": True},
                    extra_tools=extra, env_search="true")
    if a.ask:
        d1 = dataclasses.replace(d1, model="sonnet")
    await run("D1 延迟开 + 三个 server alwaysLoad=true + probe_pinned 单工具钉住", d1, ask=a.ask)
    d2 = with_flags(base, {"galatea": False}, extra_tools=extra, env_search=None)
    await run("D2 不设 ENABLE_TOOL_SEARCH + galatea alwaysLoad=false", d2)


if __name__ == "__main__":
    asyncio.run(main())
