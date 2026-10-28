"""MCP 探活 / 工具延迟 / schema 体量探针（工具挂载方案选型用，不进 CI）。

测三件事：
  1  connect() 之后 get_mcp_status() 会不会出现 pending、多久定下来；
     每个 server 实际给出的工具 vs build_options 预期挂载的工具，对不对得上。
  2  延迟模式（ENABLE_TOOL_SEARCH）下 toggle_mcp_server 关/开一个 server，
     CLI 那边的工具名单（get_context_usage.mcpTools）跟不跟着变；
     --ask 时再问模型本人：ToolSearch 取过一次之后第二次用还取不取、
     toggle 之后它眼里的名单变没变。
  3  get_context_usage()：工具 schema / 系统提示各占多少 token（全量 vs 延迟）。

挂载口径＝chat_loop.build_options(cass, handle=假把手) 的真实 MCP 集合（Ombre / 插件 /
basics / 宠物 / skills / PR / game 进程内），外加一个进程内探针 server「probe」
（只有一个 probe_ping，toggle 拿它开刀，不碰真 server）。
权限回调和 PreToolUse 门都摘掉（探针不弹卡、不推 Bark）；cwd 用临时目录，
不碰 state/。阶段 1/3 不调模型；--ask 才打真订阅（四次短调用，系统提示换成极简）。

跑法：
    server/.venv/bin/python server/tools/mcp_probe.py [--char cass] [--ask] [--json out.json]
"""
import argparse
import asyncio
import dataclasses
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

import chat_loop  # noqa: E402
import config  # noqa: E402
import pipeline  # noqa: E402
import session_mgr  # noqa: E402
import state_store  # noqa: E402

PROBE_TOOL = "mcp__probe__probe_ping"
SECRET = "PING-" + secrets.token_hex(3)
REPORT: dict = {}


def probe_server() -> dict:
    """stdio 子进程（不是进程内 SDK server）：实测 SDK server 不吃 toggle——
    关是静默空操作，开直接抛「SDK servers should be handled in print.ts」。"""
    return {"type": "stdio", "command": sys.executable,
            "args": [str(Path(__file__).with_name("mcp_probe_stdio.py"))],
            "env": {"PROBE_SECRET": SECRET}}


def make_options(char_id: str, deferred: bool, cwd: str, minimal_system: bool):
    handle = session_mgr.LoopHandle(char_id=char_id, scene="probe")
    base = chat_loop.build_options(char_id, state_store.read_sticker_catalog(),
                                   handle=handle)
    servers = dict(base.mcp_servers or {})
    servers["probe"] = probe_server()
    tools = [t for t in (base.tools or []) if t != pipeline.TOOL_SEARCH_TOOL]
    tools.append(PROBE_TOOL)
    env = dict(base.env or {})
    env.pop("ENABLE_TOOL_SEARCH", None)
    if deferred:
        tools.append(pipeline.TOOL_SEARCH_TOOL)
        env["ENABLE_TOOL_SEARCH"] = "true"
    system = base.system_prompt
    if minimal_system:
        system = "你是一个测试助手。严格按指令做，回答尽量短。"
    return dataclasses.replace(
        base,
        system_prompt=system,
        cwd=cwd,
        mcp_servers=servers,
        tools=tools,
        allowed_tools=tools,          # 探针不走卡：全放行（写类我们也不会叫它调）
        can_use_tool=None,
        hooks=None,
        env=env,
    )


def expected_by_server(tools: list[str]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for t in tools:
        if t.startswith("mcp__"):
            _, srv, name = t.split("__", 2)
            out.setdefault(srv, set()).add(name)
    return out


def _tool_name(t) -> str:
    n = t.get("name", "") if isinstance(t, dict) else str(t)
    return n.rsplit("__", 1)[-1]


async def poll_status(client, timeout: float = 30.0) -> tuple[list, dict]:
    """连上之后每 0.2s 拍一次状态，记每个 server 的状态迁移时刻。"""
    t0 = time.monotonic()
    timeline: list[tuple[float, str, str]] = []
    last: dict[str, str] = {}
    final = {}
    while True:
        st = await client.get_mcp_status()
        final = st
        now = round(time.monotonic() - t0, 2)
        for s in st.get("mcpServers", []):
            if last.get(s["name"]) != s["status"]:
                timeline.append((now, s["name"], s["status"]))
                last[s["name"]] = s["status"]
        if not any(v == "pending" for v in last.values()):
            break
        if time.monotonic() - t0 > timeout:
            timeline.append((now, "*", "TIMEOUT（仍有 pending）"))
            break
        await asyncio.sleep(0.2)
    return timeline, final


def summarize_usage(u: dict) -> dict:
    by_srv: dict[str, dict] = {}
    for t in u.get("mcpTools", []) or []:
        s = by_srv.setdefault(t.get("serverName", "?"),
                              {"n": 0, "tokens": 0, "loaded": 0})
        s["n"] += 1
        s["tokens"] += int(t.get("tokens") or 0)
        s["loaded"] += 1 if t.get("isLoaded") else 0
    return {
        "total": u.get("totalTokens"),
        "categories": [(c["name"], c["tokens"], c.get("isDeferred", False))
                       for c in u.get("categories", [])],
        "mcp_by_server": by_srv,
        "system_tools": sum(int(t.get("tokens") or 0)
                            for t in (u.get("systemTools") or [])),
        "system_prompt_sections": [(s.get("name"), s.get("tokens"))
                                   for s in (u.get("systemPromptSections") or [])],
        "deferred_builtins": [t.get("name") for t in (u.get("deferredBuiltinTools") or [])],
    }


def probe_row(u: dict):
    for t in u.get("mcpTools", []) or []:
        if t.get("serverName") == "probe":
            return {"name": t.get("name"), "tokens": t.get("tokens"),
                    "isLoaded": t.get("isLoaded")}
    return None


def show_usage(tag: str, s: dict) -> None:
    print(f"  [{tag}] totalTokens={s['total']}")
    for name, tok, dfr in s["categories"]:
        print(f"      {name:<28} {tok:>7}{'  (deferred)' if dfr else ''}")
    print(f"      内置工具合计 {s['system_tools']}；延迟的内置：{s['deferred_builtins'] or '无'}")
    for srv, v in sorted(s["mcp_by_server"].items(), key=lambda kv: -kv[1]["tokens"]):
        print(f"      mcp {srv:<16} {v['n']:>3} 个  {v['tokens']:>6} tok  已加载 {v['loaded']}")


async def phase_static(char_id: str, deferred: bool, cwd: str) -> None:
    mode = "deferred" if deferred else "full"
    print(f"\n=== 阶段 1/3（{mode}）：不调模型 ===")
    opts = make_options(char_id, deferred, cwd, minimal_system=False)
    rep: dict = {}
    t0 = time.monotonic()
    async with ClaudeSDKClient(opts) as client:
        rep["connect_sec"] = round(time.monotonic() - t0, 2)
        print(f"  connect() 返回用时 {rep['connect_sec']}s")
        timeline, final = await poll_status(client)
        rep["timeline"] = timeline
        pend = [x for x in timeline if x[2] == "pending"]
        print(f"  状态迁移（相对 connect 返回后）：{'有 pending' if pend else '首拍即定，无 pending'}")
        for ts, name, status in timeline:
            print(f"      +{ts:>5}s  {name:<16} {status}")

        exp = expected_by_server(opts.tools)
        recon = []
        for s in final.get("mcpServers", []):
            got = {_tool_name(t) for t in (s.get("tools") or [])}
            want = exp.get(s["name"], set())
            missing, extra = sorted(want - got), sorted(got - want)
            row = {"server": s["name"], "status": s["status"],
                   "error": s.get("error"), "want": len(want), "got": len(got),
                   "missing": missing, "extra_not_allowed": extra}
            recon.append(row)
            flag = "OK" if s["status"] == "connected" and not missing else "!!"
            print(f"  {flag} {s['name']:<16} {s['status']:<10} 预期 {len(want):>3} 实得 {len(got):>3}"
                  + (f"  缺 {missing}" if missing else "")
                  + (f"  多(未进白名单) {extra}" if extra else "")
                  + (f"  error={s.get('error')}" if s.get("error") else ""))
        absent = sorted(set(exp) - {s["name"] for s in final.get("mcpServers", [])})
        if absent:
            print(f"  !! 预期挂了但状态里根本没有的 server：{absent}")
        rep["reconcile"] = recon
        rep["absent_servers"] = absent

        u0 = await client.get_context_usage()
        s0 = summarize_usage(u0)
        rep["usage"] = s0
        show_usage("开局", s0)
        print("  系统提示分段：", s0["system_prompt_sections"])

        # toggle 实验（两种模式都做，不调模型）
        print(f"  probe 行（开局）：{probe_row(u0)}")
        rep["toggle"] = {}
        for srv, row_of in (("probe", probe_row), ("game", lambda u: None)):
            for on in (False, True):
                tag = f"{srv}→{'on' if on else 'off'}"
                try:
                    await client.toggle_mcp_server(srv, on)
                    err = None
                except Exception as e:
                    err = str(e)
                u = await client.get_context_usage()
                st = await client.get_mcp_status()
                status = next((s["status"] for s in st["mcpServers"] if s["name"] == srv), None)
                n_tools = sum(1 for t in (u.get("mcpTools") or []) if t.get("serverName") == srv)
                rep["toggle"][tag] = {"error": err, "status": status, "mcp_rows": n_tools,
                                      "row": row_of(u), "total": u.get("totalTokens")}
                print(f"  toggle {tag:<10} status={status}  mcpTools 里 {n_tools} 行  "
                      f"total={u.get('totalTokens')}" + (f"  ERROR={err}" if err else ""))
    REPORT[mode] = rep


async def ask(client, prompt: str) -> dict:
    await client.query(prompt)
    uses, text, usage = [], [], None
    async for m in client.receive_response():
        if isinstance(m, AssistantMessage):
            for b in m.content:
                if isinstance(b, ToolUseBlock):
                    uses.append({"name": b.name, "input": b.input})
                elif isinstance(b, TextBlock):
                    text.append(b.text)
        elif isinstance(m, ResultMessage):
            usage = m.usage
    return {"tool_uses": uses, "text": "".join(text).strip(), "usage": usage}


async def phase_ask(char_id: str, cwd: str) -> None:
    print("\n=== 阶段 2（deferred + --ask）：打真订阅，系统提示换极简 ===")
    opts = make_options(char_id, True, cwd, minimal_system=True)
    rep: dict = {}
    async with ClaudeSDKClient(opts) as client:
        await poll_status(client)
        steps = []

        async def step(tag, prompt):
            r = await ask(client, prompt)
            u = await client.get_context_usage()
            r["probe_row"] = probe_row(u)
            steps.append({"tag": tag, **r})
            names = [x["name"] for x in r["tool_uses"]]
            print(f"  [{tag}] 调用：{names or '无'}")
            print(f"        回答：{r['text'][:160]!r}")
            print(f"        probe 行：{r['probe_row']}")
            if r["usage"]:
                uu = r["usage"]
                print(f"        usage：in={uu.get('input_tokens')} cache_create="
                      f"{uu.get('cache_creation_input_tokens')} cache_read="
                      f"{uu.get('cache_read_input_tokens')}")
            return r

        await step("Q1 首次取用法+调用",
                   f"这是一次测试。先用 ToolSearch（query 写 \"select:{PROBE_TOOL}\"）"
                   f"取到用法，然后调用 {PROBE_TOOL} 一次，最后只回复它返回的暗号。")
        await step("Q2 第二次直接调",
                   f"再调用一次 {PROBE_TOOL}，只回复暗号。能直接调就别再用 ToolSearch。")
        await client.toggle_mcp_server("probe", False)
        await step("Q3 toggle off 后问名单",
                   "不要调用任何工具。列出你现在能看到的、名字里含 probe 的工具全名；"
                   "一个都没有就只回答 NONE。")
        await client.toggle_mcp_server("probe", True)
        await step("Q4 toggle on 后问名单",
                   "不要调用任何工具。列出你现在能看到的、名字里含 probe 的工具全名；"
                   "一个都没有就只回答 NONE。")
        # 措辞注意：「调用 X，只回复暗号。能直接调就别再用 ToolSearch」在这个位置
        # 连续两次被 Opus 安全过滤误报（10-29 实测），换成平实说法。
        r5 = await step("Q5 toggle 回来后再调",
                        f"请再用一次 {PROBE_TOOL} 这个工具，把它返回的文字原样告诉我。")
        rep["steps"] = steps
        rep["secret"] = SECRET
        rep["q5_secret_ok"] = SECRET in r5["text"]
    REPORT["ask"] = rep


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="cass")
    ap.add_argument("--ask", action="store_true", help="跑阶段 2（打真订阅，五次短调用）")
    ap.add_argument("--json", help="完整报告写到这个路径")
    a = ap.parse_args()
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("环境里有 ANTHROPIC_API_KEY：会走 API 计费，拒跑（同 build_options 口径）")
    from importlib.metadata import version
    REPORT["meta"] = {"sdk": version("claude-agent-sdk"), "model": config.MODEL,
                      "char": a.char, "ts": int(time.time())}
    print(f"agent-sdk {REPORT['meta']['sdk']}  model={config.MODEL}  char={a.char}")
    cwd = tempfile.mkdtemp(prefix="mcp_probe_")
    await phase_static(a.char, deferred=False, cwd=cwd)
    await phase_static(a.char, deferred=True, cwd=cwd)
    if a.ask:
        await phase_ask(a.char, cwd)
    if a.json:
        Path(a.json).write_text(json.dumps(REPORT, ensure_ascii=False, indent=1,
                                           default=str), "utf-8")
        print(f"\n报告：{a.json}")


if __name__ == "__main__":
    asyncio.run(main())
