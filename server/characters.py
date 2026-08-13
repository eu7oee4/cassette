"""角色注册表：多角色化的地基（PLAN_multichar M1）。

一个角色 = server/characters/<char_id>/ 一个目录：
    persona.md   人设（--system-prompt-file 完整替换，热读）
    char.json    接线：{"display_name", "engine", "ombre": {...}}

现有 TA 收编为默认角色 "default"，处处向后兼容：
- 不带 char_id 的调用一律落默认角色（所有带角色维度的函数缺省 char_id=None）；
- 默认角色的 persona 继续认 server/persona.md（config.PERSONA_PATH）——机主改人设的
  习惯路径不动；characters/default/persona.md 存在时才优先它；
- 默认角色的 Ombre 接线继续从 .env 读（OMBRE_*），char.json 里写了才覆盖。

engine 是留给二期同居世界的缝：一期只有 "claude-code"（claude -p 子进程 + MCP 全家桶）；
"openai-compat"（{base_url, api_key_env, model}，任意 OpenAI 兼容 API）等首个非 Claude
住户入住再实现——现在传了会在 base_claude_args 处有声报错，不静默装懂。

char.json 的 ombre 段（非默认角色必填 mcp_url，其余可选）：
    {"mcp_url": "http://localhost:18001/mcp", "mcp_token": "",
     "rest_url": "", "dashboard_password": ""}
rest_url 缺省从 mcp_url 推（去掉 /mcp 尾巴），同 config 口径。
"""
import json
from pathlib import Path

from typing import Optional

import config
import state_store

DEFAULT_ID = state_store.DEFAULT_CHAR_ID
CHARS_DIR = config.BASE_DIR / "characters"

CLAUDE_ENGINE = "claude-code"


def ensure_layout() -> None:
    """启动时补齐默认角色目录（幂等）。state 侧的旧布局迁移在 state_store import 时做，
    这里只管 characters/ 侧：目录 + 一份最小 char.json。persona 不复制——默认角色
    继续热读 server/persona.md（见模块 docstring）。"""
    d = CHARS_DIR / DEFAULT_ID
    d.mkdir(parents=True, exist_ok=True)
    cj = d / "char.json"
    if not cj.exists():
        cj.write_text(json.dumps({
            "display_name": "",          # 空 = 用 settings.agent_name / config 兜底（同旧口径）
            "engine": CLAUDE_ENGINE,
        }, ensure_ascii=False, indent=2), "utf-8")


def ids() -> list[str]:
    """全部角色 id，默认角色永远在第一位（wake 遍历/列表展示都靠这个稳定顺序）。"""
    out = [DEFAULT_ID]
    if CHARS_DIR.is_dir():
        for d in sorted(CHARS_DIR.iterdir()):
            if d.is_dir() and d.name != DEFAULT_ID and (d / "char.json").exists():
                out.append(d.name)
    return out


def resolve(char_id: Optional[str]) -> str:
    """None/空串 → 默认角色；其余必须是已注册角色，不认识就 KeyError（API 层转 404）。
    严格是有意的：静默把打错的角色名当成默认角色，消息会进错人的嘴。"""
    if not char_id or char_id == DEFAULT_ID:
        return DEFAULT_ID
    if (CHARS_DIR / char_id / "char.json").exists():
        return char_id
    raise KeyError(f"没有这个角色：{char_id}")


def meta(char_id: Optional[str] = None) -> dict:
    """char.json 热读（小文件，每次现读——改接线不用重启，同 persona 口径）。坏/缺 = {}。"""
    try:
        return json.loads((CHARS_DIR / resolve(char_id) / "char.json").read_text("utf-8"))
    except KeyError:
        raise
    except Exception:
        return {}


def persona_path(char_id: Optional[str] = None) -> Path:
    """角色的人设文件。角色目录里有 persona.md 就用它；默认角色退回 config.PERSONA_PATH
    （= server/persona.md，机主一直在编辑的那份），其他角色没有 persona 就退 example——
    宁可用通用人设也别起不来。"""
    cid = resolve(char_id)
    p = CHARS_DIR / cid / "persona.md"
    if p.exists():
        return p
    if cid == DEFAULT_ID:
        return config.PERSONA_PATH
    return config.BASE_DIR / "persona.example.md"


def tool_menu_path(char_id: Optional[str] = None) -> Path:
    """角色的人话版能力菜单（pipeline.tool_menu_block 渲染它）。口径同 persona_path：
    角色目录里有 tool_menu.md 就用它 → 默认角色退 server/tool_menu.md（机主编辑的那份）
    → 都没有就退 example。**退 example 是必须的**：菜单缺席时 TA 在延迟模式下只看得见
    一串名字，会以为自己没那些能力（见 example 文件顶部注释），宁可用默认版。"""
    cid = resolve(char_id)
    p = CHARS_DIR / cid / "tool_menu.md"
    if p.exists():
        return p
    if cid == DEFAULT_ID:
        p = config.BASE_DIR / "tool_menu.md"
        if p.exists():
            return p
    return config.BASE_DIR / "tool_menu.example.md"


def display_name(char_id: Optional[str] = None) -> str:
    """角色名：per-char settings 的 agent_name（app 里改的）→ char.json display_name
    → config 兜底默认。默认角色的读法与旧 config.agent_name() 完全同值。"""
    cid = resolve(char_id)
    name = (state_store.load_settings(cid).get("agent_name") or "").strip()
    if name:
        return name
    name = (meta(cid).get("display_name") or "").strip()
    return name or config.AGENT_NAME_DEFAULT


def engine(char_id: Optional[str] = None) -> str:
    return (meta(char_id).get("engine") or CLAUDE_ENGINE).strip() or CLAUDE_ENGINE


def ombre_conf(char_id: Optional[str] = None) -> dict:
    """角色的 Ombre 接线：{mcp_url, mcp_token, rest_url, dashboard_password}。
    默认值 = .env 的全局配置（默认角色零配置即旧行为）；char.json 的 ombre 段逐键覆盖——
    记忆跟人走，Cass 搬进来就是在这儿指回他自己的 18001。"""
    o = meta(char_id).get("ombre") or {}
    mcp_url = (o.get("mcp_url") or "").strip() or config.OMBRE_MCP_URL
    rest_url = (o.get("rest_url") or "").strip() or mcp_url.rsplit("/mcp", 1)[0]
    return {
        "mcp_url": mcp_url,
        "mcp_token": (o.get("mcp_token") or "").strip() or config.OMBRE_MCP_TOKEN,
        "rest_url": rest_url,
        "dashboard_password": (o.get("dashboard_password") or "").strip()
                              or config.OMBRE_DASHBOARD_PASSWORD,
    }
