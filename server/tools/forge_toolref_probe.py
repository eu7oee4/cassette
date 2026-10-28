"""重铸能不能「留住取过的用法」探针（不进 CI）。

延迟模式下 ToolSearch 取到的用法在 transcript 里长这样（10-29 从真 session 抄的）：
  assistant tool_use  {name: ToolSearch, input: {query: "select:X", max_results: 1}}
  user      tool_result content=[{"type": "tool_reference", "tool_name": "X"}]
这里用 forge.render 铸一段带这对事件的历史（forge 的 tools 槽目前只收字符串回执，
铸完把那条 tool_result 的 content 换成 tool_reference 列表），resume 后：
  ① get_context_usage 里 X 的 isLoaded 是不是 True（不调模型）
  ② --ask：让模型直接调 X、不许 ToolSearch，看它取不取、调不调得成
对照组：同样的历史但不带这对事件。

--mode（§9 第 1 步补测，10-29 晚加）：
  single  原样：一条 tool_result 一个 tool_reference（probe_echo）
  multi   一条 tool_result **两个** tool_reference（probe_echo + probe_pinned），
          resume 后让模型两个都直接调（echo 带必填参数 zq_token_7，传对才算 schema 在眼前）
  ghost   铸一个**当前没挂**的工具 mcp__ghost__nothing 的 tool_reference：resume 崩不崩、
          get_context_usage 怎么报、再调正常的 probe_echo 受不受影响、问它 ghost 是什么
--multi / --ghost 时附带把 transcript 里 `api-request-shape` 的工具名单抄出来（2.1.310 新增，
发给 API 的原文）——schema 在不在模型眼前的硬证据。
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
PINNED = "mcp__probe__probe_pinned"
GHOST = "mcp__ghost__nothing"
REPORT: dict = {}


def forge_history(cwd: str, refs: list[str] | None) -> str:
    """refs=None → 对照组不带 ToolSearch；否则铸 select:a,b,… → tool_result 里 len(refs)
    个 tool_reference（一条 tool_result 多个引用就是这么铸的）。"""
    t = int(time.time()) - 600
    asst = {"role": "assistant", "ts": t + 5, "text": "好，看了一下，暗号拿到了。"}
    if refs:
        asst["tools"] = [{"name": "ToolSearch",
                          "input": {"query": "select:" + ",".join(refs),
                                    "max_results": len(refs)},
                          "result": "__TOOLREF__"}]
    msgs = [{"role": "user", "ts": t, "text": "测试：帮我看一眼探针工具。"}, asst]
    sid = forge.render(msgs, cwd=cwd)
    if refs:
        p = forge.transcript_path(cwd, sid)
        lines = p.read_text("utf-8").splitlines()
        out = []
        for ln in lines:
            ev = json.loads(ln)
            c = (ev.get("message") or {}).get("content")
            if isinstance(c, list):
                for b in c:
                    if b.get("type") == "tool_result" and b.get("content") == "__TOOLREF__":
                        b["content"] = [{"type": "tool_reference", "tool_name": r}
                                        for r in refs]
                        ev["toolUseResult"] = {"matches": list(refs),
                                               "query": "select:" + ",".join(refs),
                                               "total_deferred_tools": 0}
            out.append(json.dumps(ev, ensure_ascii=False))
        p.write_text("\n".join(out) + "\n", "utf-8")
    return sid


def _rows(u: dict, servers=("probe", "ghost")) -> list[dict]:
    return [{"name": t.get("name"), "tokens": t.get("tokens"), "isLoaded": t.get("isLoaded")}
            for t in (u.get("mcpTools") or []) if t.get("serverName") in servers]


def _shape(cwd: str, sid: str, keys=("probe", "ghost")) -> list[dict]:
    rows = []
    for r in mp.transcript_api_tools(cwd, sid):
        rows.append({"ts": r["ts"], "n_tools": r["n_tools"],
                     "mcp": {k: v for k, v in r["mcp"].items()
                             if any(f"mcp__{s}__" in k for s in keys)}})
    return rows


async def one(tag: str, with_ref: bool, ask: bool, char: str, model: str) -> None:
    # realpath：macOS 的 /var 是 /private/var 的软链，CLI 按真路径算 slug 找 transcript
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="mcp_probe_"))
    sid = forge_history(cwd, [TARGET] if with_ref else None)
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


async def multi(char: str, model: str) -> None:
    """一条 tool_result 两个 tool_reference。"""
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="mcp_probe_"))
    refs = [TARGET, PINNED]
    sid = forge_history(cwd, refs)
    opts = mp.make_options(char, deferred=True, cwd=cwd, minimal_system=True)
    extra = [t for t in refs if t not in opts.tools]
    opts = dataclasses.replace(opts, resume=sid, model=model,
                               tools=opts.tools + extra,
                               allowed_tools=opts.allowed_tools + extra)
    rep: dict = {"cwd": cwd, "sid": sid, "refs": refs, "model": model}
    async with ClaudeSDKClient(opts) as c:
        await mp.poll_status(c)
        u = await c.get_context_usage()
        rep["usage_before"] = _rows(u)
        print(f"[multi] resume 后 probe 行：{rep['usage_before']}  total={u.get('totalTokens')}")
        r = await mp.ask(c, f"测试：不要用 ToolSearch。直接调用 {TARGET}（参数传字符串 hello）"
                            f"一次，再直接调用 {PINNED} 一次。调不了就如实说原因；"
                            "调成了把两个返回原样告诉我。")
        rep["ask"] = {"tool_uses": r["tool_uses"], "text": r["text"], "result": r["result"],
                      "api_error": r["api_error"]}
        print(f"    调用：{[(x['name'], x['input']) for x in r['tool_uses']]}")
        print(f"    回答：{r['text'][:200]!r}  api_error={r['api_error']}")
        u2 = await c.get_context_usage()
        rep["usage_after"] = _rows(u2)
        print(f"    ask 后 probe 行：{rep['usage_after']}")
    rep["api_request_shape"] = _shape(cwd, sid)
    for row in rep["api_request_shape"]:
        print(f"    api-request-shape {row['ts']} tools={row['n_tools']} probe/ghost={row['mcp']}")
    names = [x["name"] for x in r["tool_uses"]]
    echo_ok = any(x["name"] == TARGET and "zq_token_7" in (x["input"] or {})
                  for x in r["tool_uses"])
    rep["verdict"] = {"no_toolsearch": "ToolSearch" not in names,
                      "echo_param_ok": echo_ok, "pinned_called": PINNED in names,
                      "echo_hello_in_text": "ECHO:hello" in r["text"]}
    print(f"    判定：{rep['verdict']}")
    REPORT["multi"] = rep


async def ghost(char: str, model: str) -> None:
    """铸一个当前没挂的工具的 tool_reference。"""
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="mcp_probe_"))
    sid = forge_history(cwd, [GHOST])
    opts = mp.make_options(char, deferred=True, cwd=cwd, minimal_system=True)
    extra = [t for t in (TARGET,) if t not in opts.tools]
    opts = dataclasses.replace(opts, resume=sid, model=model,
                               tools=opts.tools + extra,
                               allowed_tools=opts.allowed_tools + extra)
    rep: dict = {"cwd": cwd, "sid": sid, "ghost": GHOST, "model": model}
    try:
        async with ClaudeSDKClient(opts) as c:
            await mp.poll_status(c)
            rep["connect_ok"] = True
            u = await c.get_context_usage()
            rep["usage_before"] = _rows(u)
            rep["ghost_rows_before"] = [t for t in (u.get("mcpTools") or [])
                                        if "ghost" in (t.get("name") or "")]
            print(f"[ghost] resume 没崩；probe/ghost 行：{rep['usage_before']}  "
                  f"total={u.get('totalTokens')}  ghost 行={rep['ghost_rows_before']}")
            r1 = await mp.ask(c, f"测试：调用 {TARGET} 一次，参数传字符串 hello（需要的话可以先用 "
                                 "ToolSearch 取用法）。把返回原样告诉我。")
            rep["ask_echo"] = {"tool_uses": r1["tool_uses"], "text": r1["text"],
                               "result": r1["result"], "api_error": r1["api_error"]}
            print(f"    echo 调用：{[(x['name'], x['input']) for x in r1['tool_uses']]}")
            print(f"    回答：{r1['text'][:200]!r}  api_error={r1['api_error']}")
            r2 = await mp.ask(c, f"不要调用任何工具。{GHOST} 是什么工具？你现在能看到它吗？"
                                 "两句话以内回答。")
            rep["ask_ghost"] = {"tool_uses": r2["tool_uses"], "text": r2["text"],
                                "result": r2["result"], "api_error": r2["api_error"]}
            print(f"    ghost 问答：{r2['text'][:300]!r}  调用={[(x['name']) for x in r2['tool_uses']]}")
            u2 = await c.get_context_usage()
            rep["usage_after"] = _rows(u2)
    except Exception as e:
        rep["connect_ok"] = False
        rep["exception"] = f"{type(e).__name__}: {e}"
        print(f"[ghost] 炸了：{rep['exception']}")
    rep["api_request_shape"] = _shape(cwd, sid)
    for row in rep["api_request_shape"]:
        print(f"    api-request-shape {row['ts']} tools={row['n_tools']} probe/ghost={row['mcp']}")
    REPORT["ghost"] = rep


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="cass")
    ap.add_argument("--ask", action="store_true")
    ap.add_argument("--models", default="sonnet,opus")
    ap.add_argument("--mode", default="single", choices=["single", "multi", "ghost"])
    ap.add_argument("--json", help="multi/ghost 的完整报告写到这个路径")
    a = ap.parse_args()
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("环境里有 ANTHROPIC_API_KEY，拒跑")
    from importlib.metadata import version
    REPORT["meta"] = {"sdk": version("claude-agent-sdk"), "mode": a.mode, "ts": int(time.time())}
    print(f"agent-sdk {REPORT['meta']['sdk']}  mode={a.mode}")
    for model in a.models.split(","):
        if a.mode == "single":
            await one(f"{model} 对照：不带 tool_reference", False, a.ask, a.char, model)
            await one(f"{model} 实验：铸进 ToolSearch→tool_reference", True, a.ask, a.char, model)
        elif a.mode == "multi":
            await multi(a.char, model)
        else:
            await ghost(a.char, model)
    if a.json:
        Path(a.json).write_text(json.dumps(REPORT, ensure_ascii=False, indent=1,
                                           default=str), "utf-8")
        print(f"报告：{a.json}")


if __name__ == "__main__":
    asyncio.run(main())
