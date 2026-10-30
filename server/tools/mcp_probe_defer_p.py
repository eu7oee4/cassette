"""`-p` 路（wake.py / sse.py 老路那种自起子进程）的延迟开关探针（不进 CI，不调模型）。

问的只有一件事：PLAN_toolmount §7 第一条——`pipeline.tool_search_on` 的 docstring 说
「只给环境变量不挂 ToolSearch = 延迟静默不生效」「只挂 ToolSearch 不给环境变量 = 白搭一份
schema」，而 sdk 路（§1.4）实测是「延迟开不开只看 ToolSearch 挂没挂」。`-p` 路没测过，
改注释前补这一腿。

四格，参数全部从 pipeline.base_claude_args(context="wake") 真实产出改出来（跟 wake.py 同一套
mcp-config / --tools / --allowedTools），只动两个旋钮：
  P1  --tools 带 ToolSearch   + ENABLE_TOOL_SEARCH=true   （生产现状）
  P2  --tools 带 ToolSearch   + 不给环境变量
  P3  --tools 不带 ToolSearch + ENABLE_TOOL_SEARCH=true
  P4  都不给                                              （全量基线）
每格起一个 `claude -p --input-format stream-json --output-format stream-json`，发 initialize +
get_context_usage 两个 control_request（跟 agent-sdk 同一条线），读 categories / mcpTools，
然后关 stdin 收工——没有 user 消息，不打订阅。

跑法：
    server/.venv/bin/python server/tools/mcp_probe_defer_p.py [--char cass] [--only P2,P3]
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402

TS = pipeline.TOOL_SEARCH_TOOL


def cell_args(base: list[str], with_ts: bool) -> list[str]:
    args = list(base)
    has = TS in args
    if with_ts and not has:
        # 插到两处白名单末尾：--tools ... 和 --allowedTools ...（都是到下一个 -- 开头为止）
        out, i = [], 0
        while i < len(args):
            out.append(args[i])
            if args[i] in ("--tools", "--allowedTools"):
                i += 1
                while i < len(args) and not args[i].startswith("--"):
                    out.append(args[i]); i += 1
                out.append(TS)
                continue
            i += 1
        args = out
    elif not with_ts and has:
        args = [a for a in args if a != TS]
    return args


def ask_usage(args: list[str], env: dict, cwd: str, timeout: float = 60.0) -> dict:
    proc = subprocess.Popen(args + ["--input-format", "stream-json",
                                    "--output-format", "stream-json", "--verbose"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=cwd)
    assert proc.stdin and proc.stdout
    reqs = [("init", {"subtype": "initialize", "hooks": None}),
            ("usage", {"subtype": "get_context_usage"})]
    got: dict = {}
    try:
        for rid, req in reqs:
            proc.stdin.write(json.dumps({"type": "control_request", "request_id": rid,
                                         "request": req}) + "\n")
            proc.stdin.flush()
        import select
        import time
        t0 = time.monotonic()
        while "usage" not in got and time.monotonic() - t0 < timeout:
            r, _, _ = select.select([proc.stdout], [], [], 1.0)
            if not r:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "control_response":
                resp = msg.get("response") or {}
                got[resp.get("request_id")] = resp
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if "usage" not in got:
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(f"没等到 get_context_usage 回应；stderr 尾巴：{err[-800:]}")
    resp = got["usage"]
    if resp.get("subtype") != "success":
        raise RuntimeError(f"get_context_usage 失败：{resp}")
    return resp.get("response") or {}


def report(tag: str, u: dict) -> dict:
    by: dict[str, dict] = {}
    for t in u.get("mcpTools") or []:
        s = by.setdefault(t.get("serverName", "?"), {"loaded": 0, "deferred": 0, "tok_l": 0, "tok_d": 0})
        if t.get("isLoaded"):
            s["loaded"] += 1; s["tok_l"] += int(t.get("tokens") or 0)
        else:
            s["deferred"] += 1; s["tok_d"] += int(t.get("tokens") or 0)
    print(f"\n[{tag}] totalTokens={u.get('totalTokens')}")
    for c in u.get("categories", []):
        print(f"    {c['name']:<26} {c['tokens']:>7}{'  (deferred)' if c.get('isDeferred') else ''}")
    for srv, v in sorted(by.items()):
        print(f"    {srv:<14} 常驻 {v['loaded']:>3} ({v['tok_l']:>5} tok)  延迟 {v['deferred']:>3} ({v['tok_d']:>5} tok)")
    return by


CELLS = {
    "P1": (True, "true"),
    "P2": (True, None),
    "P3": (False, "true"),
    "P4": (False, None),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="cass")
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()} or set(CELLS)
    base = pipeline.base_claude_args(context="wake", char_id=a.char)
    base_env = pipeline._subprocess_env("wake")
    print("base_claude_args 里 ToolSearch:", TS in base,
          "| 环境 ENABLE_TOOL_SEARCH:", base_env.get("ENABLE_TOOL_SEARCH"))
    cwd = os.path.realpath(tempfile.mkdtemp(prefix="mcp-probe-p-"))
    summary = {}
    for tag, (with_ts, env_v) in CELLS.items():
        if tag not in only:
            continue
        env = dict(base_env)
        env.pop("ENABLE_TOOL_SEARCH", None)
        if env_v is not None:
            env["ENABLE_TOOL_SEARCH"] = env_v
        try:
            u = ask_usage(cell_args(base, with_ts), env, cwd)
        except Exception as e:  # noqa: BLE001
            print(f"\n[{tag}] 失败：{e}")
            continue
        by = report(tag, u)
        n_def = sum(v["deferred"] for v in by.values())
        n_load = sum(v["loaded"] for v in by.values())
        summary[tag] = (n_load, n_def, u.get("totalTokens"))
    print("\n== 小结（常驻 / 延迟 / totalTokens）==")
    for tag, (nl, nd, tot) in summary.items():
        with_ts, env_v = CELLS[tag]
        print(f"  {tag}  ToolSearch={'挂' if with_ts else '不挂'}  env={env_v or '无'}  →  {nl} / {nd} / {tot}")
    print(f"\n探针 cwd：{cwd}（transcript 在 ~/.claude/projects/ 下同名目录，跑完可清）")


if __name__ == "__main__":
    main()
