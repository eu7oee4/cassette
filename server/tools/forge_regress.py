"""forge 真机回归（PLAN_sdk S0/PR1）：CLI / agent-sdk 任何升级前必跑的闸门。
打真订阅（三四次极小调用），不进 CI。

自动化 §2.3 的三个实验：
  A  伪造 transcript → CLI `-p --resume` → 植入暗号被第一人称认领
  B  resume 后真实事件 append 进同一份 JSONL、二次 resume 记忆仍连贯（铸一次+自然增长）
  C  agent-sdk `resume=` 吃同一份伪造 transcript → 同样认领（两条路同一个加载器）
  D  图块腿（PR13 前置闸）：user 事件里的 image 块（API 标准形状）被 resume 加载
     且真到了模型眼前——伪造一张纯红图，问颜色答得上来才算通。挂了不冻结升级，
     但 PR13「近 K 张图回填」整条降级纯文本史（设计稿三落字）。

A/B/C 任何一步挂 = 后门 skew：冻结 CLI/agent-sdk 升级，观望同族工具（§2.4）有没有同时叫。

跑法（cwd 任意）：
    server/.venv/bin/python server/tools/forge_regress.py [--model X] [--keep] [--skip-sdk]
    [--only-images]   # 只跑 D 腿（省订阅调用）
"""
import argparse
import asyncio
import base64
import json
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forge


def cli_version() -> str:
    try:
        return subprocess.run(["claude", "--version"], capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception as e:
        return f"(拿不到: {e})"


def sdk_version() -> str:
    try:
        from importlib.metadata import version
        return version("claude-agent-sdk")
    except Exception:
        return "(未安装)"


async def sdk_resume_once(session_id: str, cwd: Path, prompt: str) -> str:
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock
    opts = ClaudeAgentOptions(resume=session_id, cwd=str(cwd))
    out = []
    async for msg in query(prompt=prompt, options=opts):
        if isinstance(msg, AssistantMessage):
            out += [b.text for b in msg.content if isinstance(b, TextBlock)]
    return "\n".join(out)


def _png_solid(r: int, g: int, b: int, w: int = 64, h: int = 64) -> bytes:
    """纯 python 造一张单色 PNG（不依赖 PIL）：给 D 腿一个可问答的探针图。"""
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([r, g, b]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def leg_images(args, results: dict) -> None:
    """D 腿：user 事件带 image 块（{"type":"image","source":{base64}}，API 标准形状，
    与真 transcript 里 tool_result 内层的图块同构）。铸文本史后把图块拼进首个 user
    事件——这一步是实验专用的手术，生产的图块铸造走 forge.render 的正门（验通了才建）。"""
    workdir = Path(tempfile.mkdtemp(prefix="forge-regress-img-"))
    now = int(time.time())
    msgs = [
        {"role": "user", "ts": now - 600,
         "text": "Here is a small picture. Look at it and remember its dominant color."},
        {"role": "assistant",
         "text": "Got it — I'm looking at the picture and I'll remember its color."},
    ]
    sid = forge.render(msgs, cwd=workdir)
    path = forge.transcript_path(workdir, sid)
    lines = path.read_text(encoding="utf-8").splitlines()
    ev = json.loads(lines[0])
    ev["message"]["content"].append({
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png",
                   "data": base64.b64encode(_png_solid(220, 20, 20)).decode()}})
    lines[0] = json.dumps(ev, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"D 铸好（含红图 user 块）：{path}")

    q = ("What was the dominant color of the picture I sent earlier? "
         "Answer with one word.")
    try:
        reply = forge.resume_once(sid, workdir, q, model=args.model)
    except Exception as e:
        reply = f"(resume 炸了: {e})"
    results["D CLI resume 图块到模型眼前"] = ("red" in reply.lower()
                                              or "红" in reply)
    print(f"D 答：{reply!r}\n")

    if not args.skip_sdk:
        try:
            reply2 = asyncio.run(sdk_resume_once(sid, workdir, q))
        except Exception as e:
            reply2 = f"(sdk resume 炸了: {e})"
        results["D2 agent-sdk 同腿"] = "red" in reply2.lower() or "红" in reply2
        print(f"D2 答：{reply2!r}\n")

    if args.keep:
        print(f"--keep：留下 {workdir} 和 {path.parent}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(path.parent, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=None, help="resume 用的模型（默认随 CLI）")
    ap.add_argument("--keep", action="store_true", help="留下临时目录和伪造 transcript")
    ap.add_argument("--skip-sdk", action="store_true", help="跳过 agent-sdk 那条腿")
    ap.add_argument("--only-images", action="store_true", help="只跑 D 图块腿")
    args = ap.parse_args()

    print(f"CLI: {cli_version()} / agent-sdk: {sdk_version()}")

    if args.only_images:
        results: dict = {}
        leg_images(args, results)
        ok = all(results.values())
        for name, passed in results.items():
            print(("✅ " if passed else "❌ ") + name)
        if not ok:
            print("\nD 腿挂了：PR13「近 K 张图回填」降级纯文本史（设计稿三），"
                  "A/B/C 腿不受影响。")
        return 0 if ok else 1

    codeword = "FORGE" + secrets.token_hex(3).upper()
    workdir = Path(tempfile.mkdtemp(prefix="forge-regress-"))
    now = int(time.time())
    msgs = [
        {"role": "user", "ts": now - 600,
         "text": "Pick a secret codeword yourself and tell me."},
        {"role": "assistant",
         "text": f"I choose {codeword} as my secret codeword. "
                 "I picked it myself because it sounds bright."},
    ]
    sid = forge.render(msgs, cwd=workdir)
    path = forge.transcript_path(workdir, sid)
    print(f"铸好：{path}\n暗号：{codeword}\n")

    results = {}

    # A：CLI resume 认领
    q1 = "What codeword did you choose earlier, and who picked it? One short sentence."
    reply = forge.resume_once(sid, workdir, q1, model=args.model)
    results["A CLI resume 第一人称认领"] = codeword.lower() in reply.lower()
    print(f"A 答：{reply!r}\n")

    # B：append 进同一 JSONL + 二次 resume 连贯
    n_events = len(path.read_text().splitlines())
    grew = n_events > len(msgs)
    reply2 = forge.resume_once(sid, workdir, "Say only the codeword once more.",
                               model=args.model)
    results["B append+二次 resume 连贯"] = grew and codeword.lower() in reply2.lower()
    print(f"B 事件数 {len(msgs)} -> {n_events}，二次答：{reply2!r}\n")

    # C：agent-sdk 同一加载器
    if args.skip_sdk:
        print("C 跳过（--skip-sdk）")
    else:
        reply3 = asyncio.run(sdk_resume_once(sid, workdir, q1))
        results["C agent-sdk resume 认领"] = codeword.lower() in reply3.lower()
        print(f"C 答：{reply3!r}\n")

    # D：图块腿（PR13 前置闸）
    leg_images(args, results)

    if args.keep:
        print(f"--keep：留下 {workdir} 和 {path.parent}")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(path.parent, ignore_errors=True)

    ok = all(results.values())
    for name, passed in results.items():
        print(("✅ " if passed else "❌ ") + name)
    if not ok:
        print("\n后门 skew！冻结 CLI/agent-sdk 升级，先查同族工具动静（PLAN_sdk §2.4）。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
