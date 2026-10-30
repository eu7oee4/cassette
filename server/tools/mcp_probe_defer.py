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

§9 第 1 步补的两组（10-29 晚，2.1.310；alwaysLoad 语义 2.1.285/2.1.287 改过）：
  D3  D1 形状 + probe(stdio) 设 alwaysLoad=true，而 probe_echo 带 _meta=false（opt-out）：
      预期 probe_ping / probe_pinned 常驻、probe_echo 延迟。
  D4  D1 形状 + galatea 设 alwaysLoad=false：看 galatea 跟 D1 比有没有区别
      （D1 里 galatea 本来就延迟，差别只能从行数 / token 上看）。
--only D3,D4 只跑指定组。
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


RESULTS: dict = {}


async def run(tag, opts, ask=False):
    async with ClaudeSDKClient(opts) as c:
        await mp.poll_status(c)
        u = await c.get_context_usage()
        by = report(tag, u)
        RESULTS[tag] = {"total": u.get("totalTokens"),
                        "categories": [(x["name"], x["tokens"], x.get("isDeferred", False))
                                       for x in u.get("categories", [])],
                        "by_server": by}
        if ask:
            r = await mp.ask(c, "这是一次测试。直接调用 mcp__basics__now（不要用 ToolSearch），"
                                "然后用一句话告诉我它返回的时间。")
            print(f"    ask：调用 {[x['name'] for x in r['tool_uses']]}  回答 {r['text'][:80]!r}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="cass")
    ap.add_argument("--ask", action="store_true")
    ap.add_argument("--only", default="D1,D2,D3,D4")
    ap.add_argument("--json")
    a = ap.parse_args()
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("环境里有 ANTHROPIC_API_KEY，拒跑")
    only = set(a.only.split(","))
    cwd = tempfile.mkdtemp(prefix="mcp_probe_")
    base = mp.make_options(a.char, deferred=True, cwd=cwd, minimal_system=True)
    extra = ["mcp__probe__probe_pinned", "mcp__probe__probe_echo"]
    d1_flags = {"ombre-brain": True, "basics": True, "game": True}
    if "D1" in only:
        d1 = with_flags(base, d1_flags, extra_tools=extra, env_search="true")
        if a.ask:
            d1 = dataclasses.replace(d1, model="sonnet")
        await run("D1 延迟开 + 三个 server alwaysLoad=true + probe_pinned 单工具钉住", d1, ask=a.ask)
    if "D2" in only:
        d2 = with_flags(base, {"galatea": False}, extra_tools=extra, env_search=None)
        await run("D2 不设 ENABLE_TOOL_SEARCH + galatea alwaysLoad=false", d2)
    if "D3" in only:
        d3 = with_flags(base, {**d1_flags, "probe": True}, extra_tools=extra, env_search="true")
        await run("D3 D1 + probe alwaysLoad=true（probe_echo _meta=false opt-out）", d3)
    if "D4" in only:
        d4 = with_flags(base, {**d1_flags, "galatea": False}, extra_tools=extra, env_search="true")
        await run("D4 D1 + galatea alwaysLoad=false", d4)
    if "D5" in only:
        # 全量形状（不挂 ToolSearch、不设环境变量）+ galatea alwaysLoad=false：
        # 「false=整个 server 延迟」在没有 ToolSearch 可取的形状下还认不认。
        full = mp.make_options(a.char, deferred=False, cwd=cwd, minimal_system=True)
        d5 = with_flags(full, {"galatea": False}, extra_tools=extra, env_search=None)
        await run("D5 全量（无 ToolSearch）+ galatea alwaysLoad=false", d5)
    if a.json:
        import json
        Path(a.json).write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1, default=str), "utf-8")
        print(f"报告：{a.json}")


if __name__ == "__main__":
    asyncio.run(main())
