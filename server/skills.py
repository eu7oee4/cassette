"""skill 库：方法层的渐进披露（PLAN_skills.md）。

skill = server/skills/<name>/ 一个目录：SKILL.md（路由表+铁律，frontmatter 带
name/description/可选 when）+ prompts|frameworks/ 等子文件（按需读）。
角色覆盖：server/characters/<id>/skills/<name>/ 存在则**整目录**盖掉同名共享 skill，
其余共享（union）。比 persona/tool_menu 的整文件退落细一级——skill 是目录族，
按 skill 为单位覆盖才不会为改一个 skill 把整库复制一份。

三个场景共用这一份：chat/wake 走 pipeline（索引由 tool_menu_block 追加、正文走
skills_mcp 的 skill_read）；code 走 code_bridge（索引插 addendum 链守则之前，
同一个 MCP 挂进交互会话）。**本模块不 import pipeline/code_bridge/app**——
两边都要 import 它，谁都不能反过来牵（code_bridge 的防环红线）。

frontmatter 极简，只认三个键（别的键忽略，不报错——给以后留缝）：
    ---
    name: jobhunt          ← 必须 = 目录名，对不上整个 skill 当坏的跳过（有声）
    description: …一句话…   ← 索引里的那一行。**必须写成动作式触发**
    when: chat, wake       ← 可选；缺省 = chat/wake/code 三个场景都在
    ---
description 为什么必须动作式（「遇到 X → 先 skill_read」而不是「求职技巧」）：
条件式标题的死法 08-13 在能力菜单上实锤过（见 pipeline.TOOL_SEARCH_CONTEXTS 那段）——
TA 不觉得自己需要手册的时候，手册就等于不存在。
"""
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Optional

import config
import state_store
from notify import logerr

SKILLS_DIR = config.BASE_DIR / "skills"

# skill_read 的工具全名（挂载白名单 / 菜单 needs / code 的 permissions.allow 三处共用，
# 单一来源——手写第二份的名字总有一天会和 FastMCP("skills") 对不上）。
SKILLS_MCP_TOOLS = ["mcp__skills__skill_read"]

CONTEXTS = ("chat", "wake", "code")

_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")

_warned: set = set()   # 坏 skill 每个只喊一次，别每轮渲染都刷屏


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        logerr(msg)


def _parse_front(text: str) -> dict:
    """frontmatter → dict。没有 frontmatter / 格式坏 = {}（调用方按缺键处理）。"""
    m = _FRONT_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip().lower()] = v.strip()
    return out


def _char_skills_dir(char_id: Optional[str]) -> Optional[Path]:
    """角色的覆盖目录。角色不认识就当没有覆盖（skill 清单不该因为一个打错的
    char_id 整个炸掉——那会让能力菜单渲染失败，比少一层覆盖严重得多）。"""
    import characters
    try:
        cid = characters.resolve(char_id)
    except KeyError:
        return None
    return characters.CHARS_DIR / cid / "skills"


def _meta_of(d: Path) -> Optional[dict]:
    """一个 skill 目录 → {name, description, when(set), dir}。坏的返回 None（有声）。"""
    sk = d / "SKILL.md"
    try:
        front = _parse_front(sk.read_text("utf-8"))
    except OSError:
        return None                     # 没有 SKILL.md 的目录不是 skill，静默跳过
    name = front.get("name", "")
    desc = front.get("description", "")
    if name != d.name or not _NAME_RE.match(name):
        _warn_once(f"name:{d}", f"skill「{d.name}」的 frontmatter name 对不上目录名"
                                f"（{name!r}）——整个跳过，改一致才挂")
        return None
    if not desc:
        _warn_once(f"desc:{d}", f"skill「{d.name}」没写 description——索引里没有它那一行，"
                                f"TA 看不见它，等于没装。补一句动作式触发")
        return None
    when = {w.strip().lower() for w in front.get("when", "").split(",") if w.strip()}
    bad = when - set(CONTEXTS)
    if bad:
        _warn_once(f"when:{d}", f"skill「{d.name}」的 when 里有认不出的场景 {sorted(bad)}"
                                f"（认识的：{'/'.join(CONTEXTS)}）——已忽略这几个")
        when -= bad
    return {"name": name, "description": desc,
            "when": when or set(CONTEXTS), "dir": d}


def _skill_dirs(char_id: Optional[str] = None) -> dict:
    """{skill 名: 目录}。共享库打底，角色覆盖目录按名字整目录顶掉。"""
    out: dict = {}
    roots = [SKILLS_DIR]
    cd = _char_skills_dir(char_id)
    if cd is not None:
        roots.append(cd)                # 后写的赢 → 角色覆盖生效
    for root in roots:
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if d.is_dir() and (d / "SKILL.md").exists():
                out[d.name] = d
    return out


def list_skills(context: Optional[str] = None, char_id: Optional[str] = None) -> list[dict]:
    """这个角色在这个场景下可见的 skill 清单（按名字排序）。context=None 不过滤。
    热读：每次现扫目录——改 skill 文件不用重启（口径同 persona/tool_menu）。"""
    out = []
    for name, d in _skill_dirs(char_id).items():
        m = _meta_of(d)
        if m is None:
            continue
        if context and context not in m["when"]:
            continue
        out.append(m)
    return sorted(out, key=lambda m: m["name"])


def index_block(context: str, char_id: Optional[str] = None,
                tool_full: str = SKILLS_MCP_TOOLS[0]) -> str:
    """skill 索引块（能力菜单体例：■ 标题 + 缩进正文 + 工具行）。没有可见 skill 返回空串。
    chat/wake 由 pipeline.tool_menu_block 追加；code 由 code_bridge 插进 addendum 链——
    同一段文字，三个场景说法一致，TA 不用按场景记两套规矩。"""
    items = list_skills(context, char_id)
    if not items:
        return ""
    lines = ["■ 做事套路（skills）——下面这些事开工前先取流程，别凭感觉直接做",
             "  这些是沉淀好的完整流程，正文默认没加载。先调 skill_read(name) 拿到"
             "路由表和铁律，再按路由表用 skill_read(name, path) 读要用的子流程。"]
    for m in items:
        lines.append(f"  - {m['name']}：{m['description']}")
    lines.append(f"  工具：{tool_full}")
    return "\n".join(lines)


def read(name: str, path: str = "", char_id: Optional[str] = None) -> str:
    """skill 正文。path 空 = SKILL.md（路由表，先读这个）；否则读 skill 目录内的子文件。
    抛 ValueError（人话），MCP 层接住转成有声报错。"""
    dirs = _skill_dirs(char_id)
    d = dirs.get(name)
    if d is None:
        have = "、".join(sorted(dirs)) or "（一个都没有）"
        raise ValueError(f"没有叫「{name}」的 skill。现在有：{have}")
    rel = (path or "SKILL.md").strip()
    base = d.resolve()
    target = (base / rel).resolve()
    # 路径必须落在这个 skill 的目录里：防 ../ 逃逸，也防绝对路径直读任意文件。
    if not target.is_relative_to(base):
        raise ValueError(f"path 只能是 skill 目录内的相对路径，不能带 ../ 或绝对路径：{path!r}")
    if not target.is_file():
        sub = sorted(str(p.relative_to(base)) for p in base.rglob("*")
                     if p.is_file() and not p.name.startswith("."))
        raise ValueError(f"「{name}」里没有 {rel}。它有这些文件：{'、'.join(sub)}")
    text = target.read_text("utf-8")
    if len(text) > 200_000:
        raise ValueError(f"{rel} 太大（{len(text)} 字符），不该整个读——文件切小一点")
    return text


def mcp_config(char_id: Optional[str] = None) -> Path:
    """渲染 skills MCP 的 mcp-config（口径同 pipeline._pet_mcp_config：按角色分文件、
    原子替换、内容没变不重写）。stdio 用本仓 venv 起 skills_mcp.py；env 下发角色身份。
    pipeline（chat/wake）和 code_bridge（code）都从这儿拿，两边永远同一份接线。"""
    me = char_id or state_store.DEFAULT_CHAR_ID
    path = state_store.char_state_dir(char_id) / "skills.mcp.json"
    payload = json.dumps({"mcpServers": {"skills": {
        "type": "stdio", "command": sys.executable,
        "args": [str(config.BASE_DIR / "skills_mcp.py")],
        "env": {"CASSETTE_CHAR_ID": me}}}})
    if not path.exists() or path.read_text("utf-8") != payload:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(path)
    return path
