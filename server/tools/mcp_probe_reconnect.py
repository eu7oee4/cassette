"""`reconnect_mcp_server` 之后 Opus 调用会不会撞 §1.3 那种安全误报（PLAN_toolmount §5.3 /
§9 第 1 步；不进 CI）。

形状照 mcp_probe.py --ask：延迟模式、极简系统提示、probe 是 stdio 子进程。每次独立开一个
session：
  connect → 拍到无 pending → reconnect_mcp_server("probe")（记返回 / 异常、重连后状态）
  → Q1：ToolSearch 取 probe_ping 用法 + 调一次（看暗号）
  → 再 reconnect 一次 → Q2：照 Q5 措辞再调一次（这是 §1.3 撞误报的位置）
每条问答记 ResultMessage.is_error / result 原话；正文里出现「safeguards」或以「API Error」开头
都算撞。

跑法：
    server/.venv/bin/python server/tools/mcp_probe_reconnect.py [--model opus] [--runs 3] [--json out]
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

REPORT: dict = {}


def _probe_status(st: dict) -> dict:
    for s in st.get("mcpServers", []):
        if s["name"] == "probe":
            return {"status": s["status"], "error": s.get("error"),
                    "n_tools": len(s.get("tools") or [])}
    return {"status": None}


async def reconnect(c, tag: str, rec: dict) -> None:
    t0 = time.monotonic()
    try:
        r = await c.reconnect_mcp_server("probe")
        rec[tag] = {"ok": True, "ret": repr(r), "sec": round(time.monotonic() - t0, 2)}
    except Exception as e:
        rec[tag] = {"ok": False, "exc": f"{type(e).__name__}: {e}"[:300],
                    "sec": round(time.monotonic() - t0, 2)}
    tl, st = await mp.poll_status(c, timeout=10)
    rec[tag]["after"] = _probe_status(st)
    rec[tag]["timeline"] = tl
    print(f"  {tag}: {rec[tag]}")


async def one_run(i: int, char: str, model: str) -> dict:
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="mcp_probe_"))
    opts = mp.make_options(char, deferred=True, cwd=cwd, minimal_system=True)
    opts = dataclasses.replace(opts, model=model)
    rec: dict = {"run": i, "cwd": cwd, "model": model}
    print(f"\n=== run {i} ({model}) ===")
    async with ClaudeSDKClient(opts) as c:
        _, st = await mp.poll_status(c)
        rec["before"] = _probe_status(st)
        print(f"  开场 probe：{rec['before']}")
        await reconnect(c, "reconnect_1", rec)
        r1 = await mp.ask(c, f"这是一次测试。先用 ToolSearch（query 写 \"select:{mp.PROBE_TOOL}\"）"
                             f"取到用法，然后调用 {mp.PROBE_TOOL} 一次，最后只回复它返回的暗号。")
        rec["q1"] = {"tool_uses": [x["name"] for x in r1["tool_uses"]], "text": r1["text"][:300],
                     "result": r1["result"], "api_error": r1["api_error"],
                     "secret_ok": mp.SECRET in r1["text"]}
        print(f"  Q1 调用={rec['q1']['tool_uses']} secret_ok={rec['q1']['secret_ok']} "
              f"api_error={r1['api_error']}  text={r1['text'][:120]!r}")
        if r1["api_error"]:
            print(f"     result={r1['result']}")
        await reconnect(c, "reconnect_2", rec)
        r2 = await mp.ask(c, f"请再用一次 {mp.PROBE_TOOL} 这个工具，把它返回的文字原样告诉我。")
        rec["q2"] = {"tool_uses": [x["name"] for x in r2["tool_uses"]], "text": r2["text"][:300],
                     "result": r2["result"], "api_error": r2["api_error"],
                     "secret_ok": mp.SECRET in r2["text"]}
        print(f"  Q2 调用={rec['q2']['tool_uses']} secret_ok={rec['q2']['secret_ok']} "
              f"api_error={r2['api_error']}  text={r2['text'][:120]!r}")
        if r2["api_error"]:
            print(f"     result={r2['result']}")
        rec["sid"] = (r2["result"] or {}).get("session_id") or (r1["result"] or {}).get("session_id")
    return rec


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="cass")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--json")
    a = ap.parse_args()
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("环境里有 ANTHROPIC_API_KEY，拒跑")
    from importlib.metadata import version
    REPORT["meta"] = {"sdk": version("claude-agent-sdk"), "model": a.model,
                      "secret": mp.SECRET, "ts": int(time.time())}
    REPORT["runs"] = []
    for i in range(1, a.runs + 1):
        try:
            REPORT["runs"].append(await one_run(i, a.char, a.model))
        except Exception as e:
            REPORT["runs"].append({"run": i, "exception": f"{type(e).__name__}: {e}"[:400]})
            print(f"  run {i} 炸了：{type(e).__name__}: {e}")
    flagged = [(r.get("run"), k) for r in REPORT["runs"] for k in ("q1", "q2")
               if (r.get(k) or {}).get("api_error")]
    REPORT["flagged"] = flagged
    print(f"\n撞误报的（run, 问）：{flagged or '无'}")
    if a.json:
        Path(a.json).write_text(json.dumps(REPORT, ensure_ascii=False, indent=1,
                                           default=str), "utf-8")
        print(f"报告：{a.json}")


if __name__ == "__main__":
    asyncio.run(main())
