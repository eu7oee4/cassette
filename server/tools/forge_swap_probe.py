"""E 腿探针：**从旧 transcript 重铸**能不能带上 thinking / tool 块（PLAN_native §14.4）。

问的是一件事：把一份真 transcript 截断到轮边界、改写 session id、`--resume`——
① 原样保留带签名的 thinking 块，会不会 400？
② 保留成对的 tool_use/tool_result，会不会 400？工具调用真到模型眼前了吗？

三个变体（同一段真历史，同一个 cwd，三个 session id）：
    A  thinking ✔  tool ✔      ← 最激进：照抄
    B  thinking ✘  tool ✔      ← 14.4 想要的形状
    C  thinking ✘  tool ✘      ← 对照组＝今天 forge 的形状，必须通

只读源 transcript（**绝不写回**），产物全在临时 cwd 的 project 目录里，跑完删。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import forge

# 用法：python tools/forge_swap_probe.py <某份 ~/.claude/projects/<仓>/<session>.jsonl>
SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else None
WANT_EVENTS = 20          # 截多少条消息事件（含 tool_result 那些 user 槽）
MODEL = "claude-opus-5"

# 探针问的是**只有工具块里才有的东西**（文件路径），不是从正文能推出来的。
# 问「最后调了什么工具」不行：C 变体那条收场白里写着「改好了…DEB7B8 → #8C6E6E」，
# 模型能猜出自己用了 Edit——那样 C 就不是对照组了。路径在正文里一个字都没出现。
PROBE = ("只回答这一个问题，别用任何工具、别做任何别的事：\n"
         "在你上面这段对话历史里，你**最后一次工具调用**动的是哪个文件？"
         "给出完整路径，别的都不用说。"
         "如果你在历史里看不到任何工具调用，就只回答「看不到」。")


def load_msg_events(path: Path) -> list:
    out = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("type") not in ("user", "assistant"):
            continue
        if e.get("isSidechain"):
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, str):
            e["message"]["content"] = [{"type": "text", "text": c}]
        elif not isinstance(c, list):
            continue
        out.append(e)
    return out


def blocks(e):
    return (e.get("message") or {}).get("content") or []


def has_text(e):
    return any(b.get("type") == "text" and (b.get("text") or "").strip() for b in blocks(e))


def cut_at_turn_boundary(evs: list, want: int) -> list:
    """从尾巴取 want 条，然后把两头修到「不孤儿」：
    头——往后推到第一个**带真文字的 user 事件**（纯 tool_result 的 user 槽不能当开头）；
    尾——往前收到最后一个 tool_use 都配上了 tool_result 的位置。"""
    # 「轮边界」的定义：**从一条真人说的话，到下一条真人说的话之前**。这样切出来的
    # 段两头都是完整的——开头是 user 文字（纯 tool_result 的 user 槽当不了开头），
    # 结尾必然是这一轮的 assistant 收场白，而不是一个没人回应的 tool_result。
    # 截在半路（尾巴上挂个 tool_result 没有下文）是我们自己造的畸形，拿它去测
    # 「照抄行不行」，测到的会是畸形的锅。
    starts = [i for i, e in enumerate(evs)
              if e.get("type") == "user" and has_text(e)]
    if len(starts) < 2:
        raise SystemExit("这份 transcript 里凑不出一个完整的轮（至少要两条真人发言）")
    best = None
    for a, b in zip(starts, starts[1:]):
        seg = evs[a:b]
        if len(seg) > want:
            continue
        if not any(x.get("type") == "tool_use"
                   for e in seg for x in blocks(e)):
            continue          # 要有工具对，不然 A/B 测不到东西
        best = seg
    if best is None:
        raise SystemExit(f"没找到 ≤{want} 条且含工具调用的完整轮，调 WANT_EVENTS")
    sl = best
    while sl:
        used = {b["id"] for e in sl for b in blocks(e)
                if b.get("type") == "tool_use" and b.get("id")}
        got = {b["tool_use_id"] for e in sl for b in blocks(e)
               if b.get("type") == "tool_result" and b.get("tool_use_id")}
        if used <= got:
            break
        sl = sl[:-1]          # 尾巴上有没等到结果的调用 → 往回收一条
    if not sl:
        raise SystemExit("收完了：这段里没有完整的工具对")
    return sl


def variant(evs: list, keep_thinking: bool, keep_tools: bool,
            merge: bool = False) -> list:
    """按变体过滤块；块被删空的事件整条丢掉。

    ⚠️ merge 只给 C 开。A/B 要的是「照抄旧 transcript」，就得**保持加载器自己
    写出来的事件结构**（每个 content block 一条事件）——合并是我们的发明，
    拿它去测「照抄行不行」就测不到真问题了。C 是对照组＝今天 forge 的形状，
    工具轮被掏空后会剩下连续同角色，那儿才该合并（forge.render 同款口径）。"""
    out = []
    for e in evs:
        keep = []
        for b in blocks(e):
            t = b.get("type")
            if t == "thinking" and not keep_thinking:
                continue
            if t in ("tool_use", "tool_result") and not keep_tools:
                continue
            keep.append(b)
        if not keep:
            continue
        e = json.loads(json.dumps(e))     # 深拷贝，别动源
        e["message"]["content"] = keep
        out.append(e)
    if not merge:
        return out
    merged = []
    for e in out:
        if merged and merged[-1].get("type") == e.get("type"):
            merged[-1]["message"]["content"] += e["message"]["content"]
        else:
            merged.append(e)
    return merged


def write_session(evs: list, cwd: Path, tag: str) -> str:
    """重写 session id / cwd / 事件链，落成一份可 --resume 的 JSONL。
    ⚠️ tool_use.id 和 tool_result.tool_use_id **一个字都不改**——那是配对键。"""
    sid = str(uuid.uuid4())
    pdir = forge.project_dir(cwd)
    pdir.mkdir(parents=True, exist_ok=True)
    os.chmod(pdir, 0o700)
    lines, parent = [], None
    for i, e in enumerate(evs):
        e = json.loads(json.dumps(e))
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{sid}:{i}"))
        e["uuid"] = uid
        e["parentUuid"] = parent
        e["sessionId"] = sid
        if "session_id" in e:
            e["session_id"] = sid
        e["cwd"] = str(cwd)
        e["isSidechain"] = False
        parent = uid
        lines.append(json.dumps(e, ensure_ascii=False, separators=(",", ":")))
    path = pdir / f"{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n", "utf-8")
    os.chmod(path, 0o600)
    size = path.stat().st_size
    print(f"  {tag}: {len(evs)} 事件 / {size/1024:.0f}KB → {path.name}")
    return sid


def resume(sid: str, cwd: Path) -> tuple[bool, str]:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)      # 订阅纪律：不走 API 计费
    p = subprocess.run(
        ["claude", "-p", PROBE, "--resume", sid, "--output-format", "json",
         "--model", MODEL],
        cwd=str(cwd), capture_output=True, text=True, timeout=300, env=env)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout)[-600:]
    try:
        return True, json.loads(p.stdout).get("result", "")
    except Exception:
        return True, p.stdout[-600:]


async def sdk_resume(sid: str, cwd: Path) -> str:
    """生产聊天路吃的是 agent-sdk 的 resume=，不是 CLI 的 --resume。
    「两条路同一个加载器」对纯文本史是实证过的（forge_regress C 腿），
    但 tool 块是新形状——别拿旧结论替它作证。"""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  TextBlock, query)
    opts = ClaudeAgentOptions(resume=sid, cwd=str(cwd))
    out = []
    async for msg in query(prompt=PROBE, options=opts):
        if isinstance(msg, AssistantMessage):
            out += [b.text for b in msg.content if isinstance(b, TextBlock)]
    return "\n".join(out)


def main() -> int:
    if SRC is None or not SRC.is_file():
        print(__doc__.strip().splitlines()[0])
        print("用法：python tools/forge_swap_probe.py <transcript.jsonl>", file=sys.stderr)
        return 2
    evs = load_msg_events(SRC)
    print(f"源 transcript：{SRC.name}，可用消息事件 {len(evs)}")
    base = cut_at_turn_boundary(evs, WANT_EVENTS)
    n_think = sum(1 for e in base for b in blocks(e) if b.get("type") == "thinking")
    n_pair = sum(1 for e in base for b in blocks(e) if b.get("type") == "tool_use")
    last_tool = [b.get("name") for e in base for b in blocks(e)
                 if b.get("type") == "tool_use"]
    print(f"截出的窗口：{len(base)} 事件，thinking {n_think} 块，工具对 {n_pair} 组")
    print(f"窗口里最后一次工具调用：{last_tool[-1] if last_tool else '（无）'}\n")

    cwd = Path(tempfile.mkdtemp(prefix="swapprobe-"))
    cases = [("A thinking✔ tool✔", True, True, False),
             ("B thinking✘ tool✔", False, True, False),
             ("C thinking✘ tool✘（对照）", False, False, True)]
    sids = {}
    print("铸：")
    for tag, kt, ku, mg in cases:
        sids[tag] = write_session(variant(base, kt, ku, mg), cwd, tag)

    print("\n跑（真订阅，每个变体一次极小调用）：")
    results = {}
    for tag, _, _, _ in cases:
        ok, out = resume(sids[tag], cwd)
        results[tag] = (ok, out.strip().replace("\n", " ")[:300])
        print(f"  {tag}\n    {'✅ 通' if ok else '❌ 挂'}  {results[tag][1]!r}\n")

    # agent-sdk 腿：只跑 A/B（C 是纯文本史，那条路早验过了），因为生产走的是它。
    # ⚠️ 另铸一份新 session：CLI 的 resume 会把刚才那一问一答 **append 进同一份
    # JSONL**（forge.py 记着这条），拿用过的 sid 再跑，测的就不是原来那份历史了。
    import asyncio
    print("agent-sdk resume=（生产聊天路吃的是这个加载器，另铸一份干净的）：")
    for tag, kt, ku, mg in cases[:2]:
        sid2 = write_session(variant(base, kt, ku, mg), cwd, "SDK " + tag)
        try:
            out = asyncio.run(sdk_resume(sid2, cwd)).strip()
            ok = True
        except Exception as e:
            out, ok = f"{type(e).__name__}: {e}"[:400], False
        results[f"SDK {tag}"] = (ok, out.replace("\n", " ")[:300])
        print(f"    {'✅ 通' if ok else '❌ 挂'}  {out[:200]!r}\n")

    print("=" * 60)
    for tag, (ok, out) in results.items():
        print(f"{'✅' if ok else '❌'} {tag}  → {out[:160]!r}")

    if "--keep" in sys.argv:
        print(f"\n--keep：留下 {cwd} 和 {forge.project_dir(cwd)}")
    else:
        shutil.rmtree(cwd, ignore_errors=True)
        shutil.rmtree(forge.project_dir(cwd), ignore_errors=True)
        time.sleep(1.5)     # CLI 收尾异步补写目录，停一拍再扫尾
        shutil.rmtree(forge.project_dir(cwd), ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
