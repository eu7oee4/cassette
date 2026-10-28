"""toggle 复核（PLAN_toolmount §1.6 两条新事实 / §9 第 1 步；不进 CI）。

形状照 mcp_probe.py --ask 的 Q1 + Q5：延迟模式、极简系统提示。
  --opus-q5 N   N 个独立 session（默认 opus）：Q1 ToolSearch+调 probe_ping → toggle probe off → on
                → Q5 措辞再调一次。记每次有没有 `API Error: ... safeguards flagged`。
  --game        进程内 game server（build_options(cass, handle=假把手) 那套）：toggle game off →
                get_mcp_status 里 disabled？→ toggle on → 模型（默认 sonnet）ToolSearch + 调一次
                `mcp__game__game_notes_read`（只读）看能不能成。

跑法：
    server/.venv/bin/python server/tools/mcp_probe_toggle.py --opus-q5 2 --game [--json out]
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

GAME_TOOL = "mcp__game__game_notes_read"
REPORT: dict = {}


def _srv(st: dict, name: str) -> dict:
    for s in st.get("mcpServers", []):
        if s["name"] == name:
            return {"status": s["status"], "error": s.get("error"), "n_tools": len(s.get("tools") or [])}
    return {"status": "ABSENT_FROM_STATUS"}


def _n_rows(u: dict, server: str) -> int:
    return sum(1 for t in (u.get("mcpTools") or []) if t.get("serverName") == server)


async def _toggle(c, server: str, on: bool) -> dict:
    try:
        await c.toggle_mcp_server(server, on)
        err = None
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:300]
    st = await c.get_mcp_status()
    u = await c.get_context_usage()
    row = {"on": on, "error": err, "status": _srv(st, server), "mcp_rows": _n_rows(u, server),
           "total": u.get("totalTokens")}
    print(f"  toggle {server}→{'on' if on else 'off'}：{row}")
    return row


async def q5_run(i: int, char: str, model: str) -> dict:
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="mcp_probe_"))
    opts = dataclasses.replace(mp.make_options(char, deferred=True, cwd=cwd, minimal_system=True),
                               model=model)
    rec: dict = {"run": i, "model": model, "cwd": cwd}
    print(f"\n=== Q5 run {i} ({model}) ===")
    async with ClaudeSDKClient(opts) as c:
        await mp.poll_status(c)
        r1 = await mp.ask(c, f"这是一次测试。先用 ToolSearch（query 写 \"select:{mp.PROBE_TOOL}\"）"
                             f"取到用法，然后调用 {mp.PROBE_TOOL} 一次，最后只回复它返回的暗号。")
        rec["q1"] = {"tool_uses": [x["name"] for x in r1["tool_uses"]], "text": r1["text"][:300],
                     "result": r1["result"], "api_error": r1["api_error"],
                     "secret_ok": mp.SECRET in r1["text"]}
        print(f"  Q1 调用={rec['q1']['tool_uses']} secret_ok={rec['q1']['secret_ok']} "
              f"api_error={r1['api_error']}  text={r1['text'][:120]!r}")
        rec["off"] = await _toggle(c, "probe", False)
        rec["on"] = await _toggle(c, "probe", True)
        r5 = await mp.ask(c, f"请再用一次 {mp.PROBE_TOOL} 这个工具，把它返回的文字原样告诉我。")
        rec["q5"] = {"tool_uses": [x["name"] for x in r5["tool_uses"]], "text": r5["text"][:300],
                     "result": r5["result"], "api_error": r5["api_error"],
                     "secret_ok": mp.SECRET in r5["text"]}
        print(f"  Q5 调用={rec['q5']['tool_uses']} secret_ok={rec['q5']['secret_ok']} "
              f"api_error={r5['api_error']}  text={r5['text'][:120]!r}")
        if r5["api_error"]:
            print(f"     result={r5['result']}")
        rec["sid"] = (r5["result"] or {}).get("session_id")
    return rec


async def game_run(char: str, model: str) -> dict:
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="mcp_probe_"))
    opts = dataclasses.replace(mp.make_options(char, deferred=True, cwd=cwd, minimal_system=True),
                               model=model)
    if GAME_TOOL not in opts.tools:
        sys.exit(f"{GAME_TOOL} 不在挂载集里（_game_capable 为假？）")
    rec: dict = {"model": model, "cwd": cwd}
    print(f"\n=== game toggle ({model}) ===")
    async with ClaudeSDKClient(opts) as c:
        _, st = await mp.poll_status(c)
        u = await c.get_context_usage()
        rec["before"] = {"status": _srv(st, "game"), "mcp_rows": _n_rows(u, "game"),
                         "total": u.get("totalTokens")}
        print(f"  开场 game：{rec['before']}")
        rec["off"] = await _toggle(c, "game", False)
        rec["on"] = await _toggle(c, "game", True)
        r = await mp.ask(c, f"这是一次测试。先用 ToolSearch（query 写 \"select:{GAME_TOOL}\"）取到用法，"
                            f"然后调用 {GAME_TOOL} 一次（它没有参数），把返回的前 60 个字原样告诉我。")
        rec["ask"] = {"tool_uses": [(x["name"], x["input"]) for x in r["tool_uses"]],
                      "text": r["text"][:300], "result": r["result"], "api_error": r["api_error"]}
        print(f"  ask：调用 {rec['ask']['tool_uses']}  api_error={r['api_error']}  回答 {r['text'][:160]!r}")
        u2 = await c.get_context_usage()
        rec["after_ask"] = {"loaded": [t["name"].rsplit("__", 1)[-1] for t in (u2.get("mcpTools") or [])
                                       if t.get("serverName") == "game" and t.get("isLoaded")]}
        print(f"  ask 后 game 已加载：{rec['after_ask']}")
        rec["sid"] = (r["result"] or {}).get("session_id")
        rec["called_ok"] = any(x["name"] == GAME_TOOL for x in r["tool_uses"]) and not r["api_error"]
    return rec


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="cass")
    ap.add_argument("--opus-q5", type=int, default=0, help="跑 N 次 Q5（opus）")
    ap.add_argument("--q5-model", default="opus")
    ap.add_argument("--game", action="store_true")
    ap.add_argument("--game-model", default="sonnet")
    ap.add_argument("--json")
    a = ap.parse_args()
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("环境里有 ANTHROPIC_API_KEY，拒跑")
    from importlib.metadata import version
    REPORT["meta"] = {"sdk": version("claude-agent-sdk"), "secret": mp.SECRET, "ts": int(time.time())}
    REPORT["q5"] = []
    for i in range(1, a.opus_q5 + 1):
        try:
            REPORT["q5"].append(await q5_run(i, a.char, a.q5_model))
        except Exception as e:
            REPORT["q5"].append({"run": i, "exception": f"{type(e).__name__}: {e}"[:400]})
            print(f"  run {i} 炸了：{type(e).__name__}: {e}")
    if a.game:
        try:
            REPORT["game"] = await game_run(a.char, a.game_model)
        except Exception as e:
            REPORT["game"] = {"exception": f"{type(e).__name__}: {e}"[:400]}
            print(f"  game 炸了：{type(e).__name__}: {e}")
    flagged = [(r.get("run"), k) for r in REPORT["q5"] for k in ("q1", "q5")
               if (r.get(k) or {}).get("api_error")]
    REPORT["q5_flagged"] = flagged
    print(f"\nQ5 撞误报的：{flagged or '无'}")
    if a.json:
        Path(a.json).write_text(json.dumps(REPORT, ensure_ascii=False, indent=1,
                                           default=str), "utf-8")
        print(f"报告：{a.json}")


if __name__ == "__main__":
    asyncio.run(main())
