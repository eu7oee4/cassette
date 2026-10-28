"""Ombre 停掉时开场对账认不认得出（PLAN_toolmount §5.1 / §9 第 1 步；不进 CI）。

不停真 Ombre：把 build_options 产出的 ombre-brain 的 url 换成一个死端口
（默认 http://127.0.0.1:1/mcp），然后：
  1  connect() 后每 0.2s 拍 get_mcp_status 直到无 pending（最多 10s）：
     记 ombre-brain 最终 status / error 原话 / tools 字段有没有、别的 server 对不对账；
  2  reconnect_mcp_server("ombre-brain") 的返回 / 异常，再拍一次状态；
  3  get_context_usage 里 ombre 的行还在不在（失败的 server 还占不占 schema）；
  4  --ask：sonnet 一次，直接调 mcp__basics__now（全量模式，不用取用法）——别的 server 还能用。
两种形状各跑一遍（full / deferred），模型只在 full 那组调一次。

跑法：
    server/.venv/bin/python server/tools/mcp_probe_ombre_dead.py [--ask] [--url http://127.0.0.1:1/mcp] [--json out]
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

import mcp_probe as mp  # noqa: E402

OMBRE = "ombre-brain"
REPORT: dict = {}


def kill_ombre(opts, url: str):
    servers = dict(opts.mcp_servers)
    if OMBRE not in servers:
        sys.exit(f"build_options 没挂 {OMBRE}（ombre_alive 为假？）——这组测不了")
    cfg = dict(servers[OMBRE])
    cfg["url"] = url
    servers[OMBRE] = cfg
    return dataclasses.replace(opts, mcp_servers=servers)


def _srv(st: dict, name: str) -> dict:
    for s in st.get("mcpServers", []):
        if s["name"] == name:
            return {"status": s["status"], "error": s.get("error"),
                    "has_tools_field": "tools" in s,
                    "n_tools": len(s.get("tools") or []),
                    "keys": sorted(s.keys())}
    return {"status": "ABSENT_FROM_STATUS"}


def _reconcile(opts, st: dict) -> list[dict]:
    exp = mp.expected_by_server(opts.tools)
    rows = []
    for s in st.get("mcpServers", []):
        got = {mp._tool_name(t) for t in (s.get("tools") or [])}
        want = exp.get(s["name"], set())
        rows.append({"server": s["name"], "status": s["status"], "want": len(want),
                     "got": len(got), "missing": sorted(want - got)[:5]})
    return rows


async def run(tag: str, opts, ask: bool) -> dict:
    rec: dict = {"tag": tag}
    print(f"\n=== {tag} ===")
    t0 = time.monotonic()
    async with ClaudeSDKClient(opts) as c:
        rec["connect_sec"] = round(time.monotonic() - t0, 2)
        tl, st = await mp.poll_status(c, timeout=10)
        rec["timeline"] = tl
        rec["settle_sec"] = tl[-1][0] if tl else None
        rec["ombre_after_connect"] = _srv(st, OMBRE)
        print(f"  connect {rec['connect_sec']}s；时间线：{tl}")
        print(f"  {OMBRE} 开场：{rec['ombre_after_connect']}")
        rec["reconcile"] = _reconcile(opts, st)
        for r in rec["reconcile"]:
            flag = "OK" if r["status"] == "connected" and not r["missing"] else "!!"
            print(f"  {flag} {r['server']:<14} {r['status']:<10} 预期 {r['want']:>3} 实得 {r['got']:>3}"
                  + (f"  缺 {r['missing']}" if r["missing"] else ""))
        u = await c.get_context_usage()
        rows = [t for t in (u.get("mcpTools") or []) if t.get("serverName") == OMBRE]
        rec["usage_ombre_rows"] = {"n": len(rows), "tokens": sum(int(t.get("tokens") or 0) for t in rows),
                                   "loaded": sum(1 for t in rows if t.get("isLoaded"))}
        rec["usage_total"] = u.get("totalTokens")
        print(f"  get_context_usage：total={u.get('totalTokens')} ombre 行={rec['usage_ombre_rows']}")

        t1 = time.monotonic()
        try:
            r = await c.reconnect_mcp_server(OMBRE)
            rec["reconnect"] = {"ok": True, "ret": repr(r), "sec": round(time.monotonic() - t1, 2)}
        except Exception as e:
            rec["reconnect"] = {"ok": False, "exc": f"{type(e).__name__}: {e}"[:400],
                                "sec": round(time.monotonic() - t1, 2)}
        print(f"  reconnect：{rec['reconnect']}")
        tl2, st2 = await mp.poll_status(c, timeout=10)
        rec["ombre_after_reconnect"] = _srv(st2, OMBRE)
        rec["timeline_after_reconnect"] = tl2
        print(f"  {OMBRE} 重连后：{rec['ombre_after_reconnect']}  时间线 {tl2}")
        others = [(s["name"], s["status"]) for s in st2.get("mcpServers", []) if s["name"] != OMBRE]
        rec["others_after_reconnect"] = others
        print(f"  其他 server：{others}")

        if ask:
            q = await mp.ask(c, "这是一次测试。直接调用 mcp__basics__now（不要用 ToolSearch），"
                                "然后用一句话告诉我它返回的时间。")
            rec["ask"] = {"tool_uses": [(x["name"], x["input"]) for x in q["tool_uses"]],
                          "text": q["text"][:300], "result": q["result"], "api_error": q["api_error"]}
            print(f"  ask：调用 {rec['ask']['tool_uses']}  api_error={q['api_error']}  回答 {q['text'][:120]!r}")
    return rec


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="cass")
    ap.add_argument("--url", default="http://127.0.0.1:1/mcp")
    ap.add_argument("--ask", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("环境里有 ANTHROPIC_API_KEY，拒跑")
    from importlib.metadata import version
    REPORT["meta"] = {"sdk": version("claude-agent-sdk"), "url": a.url, "ts": int(time.time())}
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="mcp_probe_"))
    full = kill_ombre(mp.make_options(a.char, deferred=False, cwd=cwd, minimal_system=True), a.url)
    full = dataclasses.replace(full, model="sonnet")
    REPORT["full"] = await run("full（全量）+ ombre 死端口", full, ask=a.ask)
    dfr = kill_ombre(mp.make_options(a.char, deferred=True, cwd=cwd, minimal_system=True), a.url)
    REPORT["deferred"] = await run("deferred（延迟）+ ombre 死端口", dfr, ask=False)
    if a.json:
        Path(a.json).write_text(json.dumps(REPORT, ensure_ascii=False, indent=1,
                                           default=str), "utf-8")
        print(f"报告：{a.json}")


if __name__ == "__main__":
    asyncio.run(main())
