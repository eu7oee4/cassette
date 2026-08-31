"""forge：权威消息列表 → Claude Code 落盘 transcript（PLAN_sdk §2 的引擎无关核心）。

会话落盘在 ~/.claude/projects/<slug>/<session_id>.jsonl，一行一事件；resume 加载它
时无任何签名或校验（2026-08-29 真机实证，PLAN_sdk §2.3——CLI 与 agent-sdk 吃的是
同一个加载器）。本模块把「权威消息列表 → 这份 JSONL」做成确定性纯函数：同样的
输入永远铸出字节相同的文件，铸出的每段 assistant 轮都能追溯回权威库里的原句。

红线（PLAN_sdk §2.2/§2.5，用 API 形状执行，不靠自觉）：

- render() 只接受消息列表——**没有** append、也没有读回已有 transcript 的接口。
  重铸必须从权威源重新 derive，磁盘同名文件被整个覆盖（权威盖掉一切，自愈语义）。
- 字面不改写：assistant 轮必须严格等于 TA 见过的气泡。调用方从权威源导出原文，
  本模块只管格式；场景渲染规则（带哪些消息进来）也只准决定「带不带」，不准动字面。

真机验证走 server/tools/forge_regress.py——CLI / agent-sdk 任何升级前必跑的闸门。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

PROJECTS_ROOT = Path.home() / ".claude" / "projects"

# 事件里的标注字段值。resume 不校验它们（实证），保持与验证实验一致只为少踩意外。
TRANSCRIPT_VERSION = "2.1.250"    # §2.3 验证实验当时的 CLI 版本（纯标注）
DEFAULT_MODEL = "claude-opus-5"

# §2.4 重铸窗口起点常数（同族工具 dankefox 的生产值，聊天场景基线；场景规则层调参）
KEEP_TOKENS = 50_000
KEEP_TURNS = 14

# 所有确定性 uuid 都从这个命名空间派生（ca55e77e = cassette）
_NS = uuid.UUID("ca55e77e-f069-4000-8000-000000000001")


# ---------- 路径 ----------

def slug(cwd) -> str:
    """CC 的 project 目录名规则：cwd 里非字母数字一律转 '-'（实测含 . _ /）。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def project_dir(cwd, root: Optional[Path] = None) -> Path:
    return (root or PROJECTS_ROOT) / slug(cwd)


def transcript_path(cwd, session_id: str, root: Optional[Path] = None) -> Path:
    return project_dir(cwd, root) / f"{session_id}.jsonl"


def assert_no_slug_collision(cwds: Iterable) -> None:
    """不同 cwd 铸进同一个 project 目录 = 读错别人历史 = 串台的文件系统版
    （[[cassette-charswitch-bug-class]]）。session 管理层起 loop 前先过这道。"""
    seen: dict[str, str] = {}
    for c in cwds:
        s, c = slug(c), str(c)
        if s in seen and seen[s] != c:
            raise ValueError(f"slug 撞目录：{seen[s]} 和 {c} 都落在 {s}/，换一个 cwd")
        seen[s] = c


# ---------- 窗口裁剪（只决定带不带整条消息，字面一律原样） ----------

def estimate_tokens(text: str) -> int:
    """粗算：CJK ≈ 1 token/字，其余 ≈ 4 字符/token。只喂窗口裁剪，别当计费。"""
    cjk = sum(1 for ch in text if ord(ch) >= 0x2E80)
    return cjk + (len(text) - cjk + 3) // 4


def tail_window(messages: list[dict], keep_tokens: int = KEEP_TOKENS,
                keep_turns: int = KEEP_TURNS) -> list[dict]:
    """从尾部取窗口：最多 keep_turns 个 user 轮、总量不超 keep_tokens，
    并尽量让窗口从 user 轮开头（resume 后他看到的第一句是别人对他说的话，
    不是一段悬空的自己）。"""
    total, turns, start = 0, 0, len(messages)
    for i in range(len(messages) - 1, -1, -1):
        t = estimate_tokens(messages[i].get("text", ""))
        if total + t > keep_tokens and start < len(messages):
            break
        total += t
        start = i
        if messages[i].get("role") == "user":
            turns += 1
            if turns >= keep_turns:
                break
    head = start
    while head < len(messages) and messages[head].get("role") != "user":
        head += 1
    if head < len(messages):
        start = head
    return messages[start:]


# ---------- 铸造 ----------

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _det_uuid(*parts) -> str:
    return str(uuid.uuid5(_NS, ":".join(str(p) for p in parts)))


def render(messages: list[dict], *, cwd, session_id: Optional[str] = None,
           model: str = DEFAULT_MODEL, git_branch: str = "HEAD",
           projects_root: Optional[Path] = None) -> str:
    """把权威消息列表铸成 transcript，返回 session_id（resume 用它接上）。

    messages：[{"role": "user"|"assistant", "text": str, "ts": 秒级时间戳,
    "images": [{"media_type": str, "data": b64 str}, ...]（可选，只许 user 槽）}]，
    顺序即历史。ts 允许缺省（沿用上一条的），但第一条必须有——时间是权威源里的
    事实，不在这里发明。images（PR13 图块腿 2026-08-30 真机验通：CLI/agent-sdk
    两条路 resume 都真到模型眼前）：字节参与 session digest（确定性），数据由
    调用方从账本引用读出——render 仍是纯函数，不读盘。
    写盘原子（临时文件 + rename），文件 600 / 目录 700。
    """
    if not messages:
        raise ValueError("空消息列表没有可铸的历史")
    norm: list[tuple[str, str, float, tuple]] = []
    ts: Optional[float] = None
    for i, m in enumerate(messages):
        role, text = m.get("role"), m.get("text")
        if role not in ("user", "assistant"):
            raise ValueError(f"第 {i} 条 role={role!r}：只有 user/assistant 能进 messages"
                             "（世界事件走文档侧，PLAN_sdk §5.2）")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"第 {i} 条 text 为空——权威源导出就不该有空气泡")
        ts = m.get("ts", ts)
        if ts is None:
            raise ValueError("第一条消息必须带 ts（时间是权威源的事实，不在这里发明）")
        images = []
        for j, img in enumerate(m.get("images") or []):
            if role != "user":
                raise ValueError(f"第 {i} 条：image 块只许铸 user 槽（他产的是文字，"
                                 "图是他看到的——见闻侧，PLAN_sdk §4）")
            mt, data = img.get("media_type"), img.get("data")
            if not mt or not isinstance(data, str) or not data.strip():
                raise ValueError(f"第 {i} 条第 {j} 张图缺 media_type/data")
            images.append((str(mt), data))
        norm.append((role, text, float(ts), tuple(images)))

    # 连续同角色合并成一轮（08-30 game 实锤的污染根修）：铸出来的历史必须长得像
    # 引擎自己会写的历史——严格 user/assistant 交替。游戏点评那种一口气几十条
    # assistant 铸成几十个连续事件后，CLI 加载时会在缝里塞合成 user 槽（空槽+
    # token 余量标记+截断提示），模型看满屏这种缝就学舌，把「user·system<total_
    # tokens>…」缀在自己每段话结尾，投递→再铸→自我放大。合并=逐字拼接（\n\n），
    # 字面不动，红线合规（口径同 sse 把一轮多段拼成 full_reply）。
    merged: list[tuple[str, str, float, tuple]] = []
    for role, text, t, imgs in norm:
        if merged and merged[-1][0] == role:
            prev_role, prev_text, prev_t, prev_imgs = merged[-1]
            merged[-1] = (prev_role, prev_text + "\n\n" + text, prev_t,
                          prev_imgs + imgs)
        else:
            merged.append((role, text, t, imgs))
    norm = merged

    # 校验矩阵（PLAN_sdk 设计稿三 / §2.4 Forge Reload 入账，出现即失败）：
    # 首事件尽量 user——铸出的历史必须长得像引擎自己会写的（首位 assistant 时
    # CLI 会在顶上垫合成 user 槽，同「缝隙学舌」一类）。有 user 可去头就去
    # （渲染规则只决定「带不带」，字面不动）；全程没有 user（罕见：纯醒来独白
    # 窗口）保持原样，顶垫一枚认了，scrub_seam 在投递侧兜学舌。
    if any(r == "user" for r, _, _, _ in norm):
        while norm and norm[0][0] != "user":
            norm.pop(0)

    if session_id is None:
        digest = hashlib.sha256(
            json.dumps([slug(cwd)] + [[r, t, s,
                                       [hashlib.sha256(d.encode()).hexdigest()
                                        for _, d in imgs]]
                                      for r, t, s, imgs in norm],
                       ensure_ascii=False).encode()).hexdigest()
        session_id = _det_uuid("session", digest)

    lines = []
    parent: Optional[str] = None
    for i, (role, text, t, imgs) in enumerate(norm):
        uid = _det_uuid(session_id, "evt", i)
        if role == "user":
            content = [{"type": "text", "text": text}]
            content += [{"type": "image",
                         "source": {"type": "base64", "media_type": mt, "data": d}}
                        for mt, d in imgs]
            ev = {
                "parentUuid": parent, "isSidechain": False,
                "promptId": _det_uuid(session_id, "prompt", i),
                "type": "user",
                "message": {"role": "user", "content": content},
                "uuid": uid, "timestamp": _iso(t),
                "permissionMode": "default", "promptSource": "sdk",
                "userType": "external", "entrypoint": "sdk-cli",
                "cwd": str(cwd), "sessionId": session_id,
                "version": TRANSCRIPT_VERSION, "gitBranch": git_branch,
            }
        else:
            ev = {
                "parentUuid": parent, "isSidechain": False,
                "message": {
                    "model": model,
                    "id": "msg_forge_" + _det_uuid(session_id, "msg", i)[:12],
                    "type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 1,
                              "output_tokens": max(1, estimate_tokens(text)),
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0},
                },
                "requestId": "req_forge_" + _det_uuid(session_id, "req", i)[:12],
                "type": "assistant",
                "uuid": uid, "timestamp": _iso(t), "effort": "high",
                "userType": "external", "entrypoint": "sdk-cli",
                "cwd": str(cwd), "sessionId": session_id,
                "version": TRANSCRIPT_VERSION, "gitBranch": git_branch,
            }
        parent = uid
        _assert_native_block_types(ev)
        lines.append(json.dumps(ev, ensure_ascii=False, separators=(",", ":")))

    pdir = project_dir(cwd, projects_root)
    pdir.mkdir(parents=True, exist_ok=True)
    os.chmod(pdir, 0o700)
    path = pdir / f"{session_id}.jsonl"
    tmp = pdir / f".{session_id}.jsonl.forge-tmp"
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)   # 覆盖写：权威盖掉磁盘上的一切（§2.5 自愈语义）
    return session_id


def _assert_native_block_types(ev: dict) -> None:
    """校验矩阵（PLAN_sdk 设计稿三，出现即失败）：content 只许 text/image——
    **永不铸 thinking**（signed thinking 伪造不了=首次请求 400，§2.4 Forge
    Reload 入账）、**不铸 tool_use/tool_result**（孤儿 tool 块同 400；Tool
    Primer 是真机撞见工具变形时的后手，不是现在的路）。这条断言防的是未来
    有人改 render 忘了这页历史。"""
    for b in ev["message"]["content"]:
        t = b.get("type")
        if t not in ("text", "image"):
            raise AssertionError(
                f"forge 铸出了禁块类型 {t!r}——校验矩阵：只许 text/image")


# ---------- 运维自检（PLAN_sdk S0/PR3：§2.5 三条纪律的机器化） ----------

# 路径里见到这些字样 = transcript 躺在云同步盘里，陈旧副本盖回会把篡改当真历史
# 继承（§2.5 风险③，自愈立断）
_SYNC_MARKERS = ("Mobile Documents", "CloudStorage", "Dropbox", "Google Drive", "OneDrive")


def ops_check(root: Optional[Path] = None, fix: bool = False,
              check_tm: bool = True) -> list[str]:
    """返回问题清单（空=健康）。查三样：① transcript 目录不在云同步盘里；
    ② Time Machine 已排除（备份回滚=陈旧盖回的另一条路）；③ 权限 600/700
    （组/其他一个位都不该有）。fix=True 顺手修能修的（tmutil addexclusion +
    chmod 收紧）；探活走只读模式，修理用 `python forge.py --fix` 手动跑。"""
    problems: list[str] = []
    root = root or PROJECTS_ROOT
    if not root.exists():
        return problems
    real = str(root.resolve())
    for m in _SYNC_MARKERS:
        if m in real:
            problems.append(f"transcript 目录在同步盘里（路径含「{m}」）：{real}")
    if check_tm and sys.platform == "darwin" and shutil.which("tmutil"):
        def _excluded() -> bool:
            r = subprocess.run(["tmutil", "isexcluded", str(root)],
                               capture_output=True, text=True, timeout=15)
            return "[Excluded]" in r.stdout
        try:
            if not _excluded():
                if fix:
                    subprocess.run(["tmutil", "addexclusion", str(root)],
                                   capture_output=True, timeout=15)
                if not (fix and _excluded()):
                    problems.append("Time Machine 没排除 transcript 目录"
                                    "（server/.venv/bin/python forge.py --fix）")
        except Exception:
            pass   # tmutil 抽风不算 transcript 有病，别把探活搞红
    loose: list[Path] = []
    dirs = [root] + [d for d in root.iterdir() if d.is_dir()]
    for d in dirs:
        if d.stat().st_mode & 0o077:
            if fix:
                os.chmod(d, d.stat().st_mode & ~0o077)
            else:
                loose.append(d)
    for d in dirs[1:]:
        for f in d.glob("*.jsonl"):
            if f.stat().st_mode & 0o077:
                if fix:
                    os.chmod(f, f.stat().st_mode & ~0o077)
                else:
                    loose.append(f)
    if loose:
        heads = ", ".join(str(p) for p in loose[:3])
        problems.append(f"{len(loose)} 个路径组/其他可读（600/700 纪律）：{heads}"
                        + ("…" if len(loose) > 3 else ""))
    return problems


# ---------- resume 帮手（真机验证 / 滚动重开 ping 用） ----------

def resume_once(session_id: str, cwd, prompt: str, *, timeout: int = 180,
                model: Optional[str] = None, claude_bin: str = "claude") -> str:
    """CLI `-p --resume` 续一轮，返回模型文本。默认不 fork：真实事件会 append 进
    同一份 transcript（§2.3 实证）。生产聊天场景别随手 ping——ping 也是历史。"""
    args = [claude_bin, "-p", prompt, "--resume", session_id, "--output-format", "json"]
    if model:
        args += ["--model", model]
    p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"claude resume 失败 rc={p.returncode}: {p.stderr[-500:]}")
    return json.loads(p.stdout).get("result", "")


if __name__ == "__main__":
    _fix = "--fix" in sys.argv
    _probs = ops_check(fix=_fix)
    for _p in _probs:
        print("❌ " + _p)
    print("✅ 运维自检干净" if not _probs else
          ("（--fix 已尽力，剩下的要手动）" if _fix else "（跑 forge.py --fix 修）"))
    sys.exit(1 if _probs else 0)
