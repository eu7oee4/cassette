"""插件系统：工具族不进主仓，各自是独立小仓（带 plugin.json 清单），
这里负责 装（git clone）/ 开关 / 卸载 / 挂载。app 的插件商店页是遥控器，
工具代码全在这台 Mac 上跑。

安全边界（README Safety Design 同步描述）：
- install = 往自己机器下载并运行代码 → registry 是**写死的白名单**（只认自己名下的仓），
  绝不接受任意 URL；插件名过正则白名单，clone 目标锁死在 plugins/ 下。
- toggle 零联网（claude 子进程每轮新起，开关下一轮即时生效）；uninstall = 删目录。
- 挂载沿用全仓安全姿态：只把清单里声明的工具加进 --tools/--allowedTools 白名单，
  内置危险工具（Bash/Write 等）永远不开。

plugin.json 清单（PR8 定稿）：
    {
      "name": "webpage",              # = 目录名/registry 键，^[a-z0-9_-]{1,40}$
      "display_name": "网页工坊",
      "description": "做/改/传送 HTML 网页",
      "version": "0.1.0",
      "entry": "webpage_mcp.py",      # 相对插件目录的 MCP stdio 入口（本仓 venv python 起）
      "tools": ["webpage_create"]     # MCP 工具名（不带 mcp__ 前缀），逐个进白名单
    }
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

import config
import state_store

PLUGINS_DIR = config.BASE_DIR / "plugins"          # 安装目录（gitignore，装的都是外部仓）
ENABLED_PATH = state_store.STATE_DIR / "plugins_enabled.json"
# 挂载清单按调用场景分文件。**不能共用一个路径**：聊天和醒来的工具集不一样（见
# NO_WAKE_PLUGINS），共用就会互相覆盖——两条路并发起子进程时，谁后写谁说了算，
# 另一边的 claude 读到的是对方那份。
MCP_CONFIG_PATHS = {
    "chat": state_store.STATE_DIR / "plugins.mcp.json",
    "wake": state_store.STATE_DIR / "plugins.wake.mcp.json",
}

# 醒来那条路不挂的插件。
#
# 这是**宿主侧的安全策略，不交给插件自己在 plugin.json 里声明**——「这个工具能不能给
# 一个没人看着的凌晨三点的进程用」是我们的判断，插件作者没有动机限制自己。
#
# codemode：一调就起一个手握整台电脑（Bash/Write/Edit）的常驻会话，权限弹窗只能靠人在
# 手机上按。聊天里切过去是人当场要的；一次随机醒来自己切进去完全是另一回事。
# mianmian 同口径（main_v2.py base_claude_args：「自切 code：只主 chat，wake 不挂」）。
NO_WAKE_PLUGINS = {"codemode"}

# 写死的插件 registry：只认自己名下的仓，且**钉死 commit**——审过哪份代码就装哪份，
# main 后续怎么动都影响不到已发版本。升级插件 = 改这里的 commit + 发版。
REGISTRY: dict[str, dict] = {
    "webpage": {
        "repo": "https://github.com/eu7oee4/cassette-plugin-webpage",
        "commit": "f77214dd2dfbe9a3fe864e2224b30b1cdd01cd3a",
        "display_name": "网页工坊",
        "description": "做 / 改 / 传送 HTML 网页（第一个插件）",
    },
    # 装这个 = 把 Code 模式的开关也交到 TA 手上（不装就只有你能按顶栏那个按钮）。
    # 它只是个转发壳，真活在 /codemode/start；护栏全在宿主侧，插件不参与：已有会话不杀旧
    # 起新、cwd 必须落在 CODE_CWD_ALLOW 里、权限弹窗一律保留、**醒来那条路不挂它**
    # （见下面 NO_WAKE_PLUGINS）。前提是后端先开了 CODE_MODE_ENABLED=1，没开调用返 503。
    "codemode": {
        "repo": "https://github.com/eu7oee4/cassette-plugin-codemode",
        "commit": "2388151f581f099405be49cac652402d0fe60b5e",
        "display_name": "自己切 Code 模式",
        "description": "让 TA 在聊天里自己切到你电脑上去干活（需后端先开启 Code 模式）",
    },
}

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_TOOL_RE = re.compile(r"^[a-zA-Z0-9_]{1,64}$")
_LOCK = threading.Lock()


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail="插件名不合法")
    return name


def _atomic_write(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def _read_enabled() -> dict:
    try:
        return json.loads(ENABLED_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _read_manifest(name: str) -> Optional[dict]:
    """读并校验插件清单。清单坏了返回 None（列表里标 invalid，不挂载）。"""
    p = PLUGINS_DIR / name / "plugin.json"
    try:
        m = json.loads(p.read_text("utf-8"))
    except Exception:
        return None
    entry = m.get("entry") or ""
    tools = m.get("tools") or []
    if (m.get("name") != name or not entry or "/" in entry or ".." in entry
            or not isinstance(tools, list) or not tools
            or not all(isinstance(t, str) and _TOOL_RE.match(t) for t in tools)
            or not (PLUGINS_DIR / name / entry).is_file()):
        return None
    return m


def list_status() -> list[dict]:
    """registry ∪ 已安装 → 三态清单（not_installed / disabled / enabled）。"""
    enabled = _read_enabled()
    out = []
    names = list(REGISTRY.keys())
    if PLUGINS_DIR.is_dir():
        for d in sorted(PLUGINS_DIR.iterdir()):
            if d.is_dir() and d.name not in names:
                names.append(d.name)   # 本地手放的插件也显示（开发用），但装不了新的
    for name in names:
        reg = REGISTRY.get(name, {})
        installed = (PLUGINS_DIR / name).is_dir()
        manifest = _read_manifest(name) if installed else None
        item = {
            "name": name,
            "display_name": (manifest or reg).get("display_name") or name,
            "description": (manifest or reg).get("description", ""),
            "version": (manifest or {}).get("version", ""),
            "in_registry": name in REGISTRY,
            "state": ("enabled" if enabled.get(name) else "disabled") if installed else "not_installed",
            "valid": manifest is not None if installed else True,
        }
        out.append(item)
    return out


def _clone_pinned(reg: dict, dest: Path) -> None:
    """clone + checkout 钉死的 commit + 校验清单。任一步失败删目录抛 HTTPException。
    插件仓都很小，不用浅 clone（checkout 任意 sha 需要完整历史）。"""
    try:
        subprocess.run(["git", "clone", reg["repo"], str(dest)],
                       capture_output=True, text=True, timeout=180, check=True)
        commit = reg.get("commit", "")
        if commit:
            subprocess.run(["git", "-C", str(dest), "checkout", "--detach", commit],
                           capture_output=True, text=True, timeout=60, check=True)
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=504, detail="clone 超时（网络问题？）")
    except subprocess.CalledProcessError as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"clone/checkout 失败：{(e.stderr or '')[:200]}")


def install(name: str) -> dict:
    """从 registry 白名单 clone 插件仓（钉 commit）。装完默认「已装未启用」。"""
    _check_name(name)
    reg = REGISTRY.get(name)
    if reg is None:
        raise HTTPException(status_code=403, detail="不在插件白名单里（registry 写死，只认自己名下的仓）")
    dest = PLUGINS_DIR / name
    with _LOCK:
        if dest.exists():
            raise HTTPException(status_code=409, detail="已安装过；要升级用更新")
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        _clone_pinned(reg, dest)
    if _read_manifest(name) is None:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=502, detail="插件清单（plugin.json）不合法，已回滚")
    return {"ok": True, "state": "disabled"}


def update(name: str) -> dict:
    """升级到 registry 当前钉的 commit：先 clone 到临时目录验清单，全绿才换掉旧目录——
    失败旧版原地不动。开关状态保留；插件数据在 state/ 下，换目录不影响。"""
    _check_name(name)
    reg = REGISTRY.get(name)
    if reg is None:
        raise HTTPException(status_code=403, detail="不在插件白名单里，没有升级来源")
    dest = PLUGINS_DIR / name
    if not dest.is_dir():
        raise HTTPException(status_code=404, detail="没装这个插件（直接安装即可）")
    tmp = PLUGINS_DIR / f".update-{name}-{uuid.uuid4().hex[:8]}"
    with _LOCK:
        _clone_pinned(reg, tmp)
        # 换上新目录再验清单（校验依赖目录名=name），不合法就换回旧版
        backup = PLUGINS_DIR / f".old-{name}-{uuid.uuid4().hex[:8]}"
        dest.rename(backup)
        tmp.rename(dest)
        if _read_manifest(name) is None:
            shutil.rmtree(dest, ignore_errors=True)
            backup.rename(dest)
            raise HTTPException(status_code=502, detail="新版本清单不合法，已回滚到旧版")
        shutil.rmtree(backup, ignore_errors=True)
    return {"ok": True}


def toggle(name: str, on: bool) -> dict:
    """开/关（零联网）。claude 子进程每轮新起，下一轮聊天/醒来即时生效。"""
    _check_name(name)
    if not (PLUGINS_DIR / name).is_dir():
        raise HTTPException(status_code=404, detail="没装这个插件")
    if on and _read_manifest(name) is None:
        raise HTTPException(status_code=502, detail="插件清单不合法，不能启用")
    with _LOCK:
        enabled = _read_enabled()
        enabled[name] = bool(on)
        _atomic_write(ENABLED_PATH, enabled)
    return {"ok": True, "state": "enabled" if on else "disabled"}


def uninstall(name: str) -> dict:
    """卸载=删目录。插件自己的数据该放 state/（按约定），不跟目录一起消失。"""
    _check_name(name)
    dest = PLUGINS_DIR / name
    if not dest.is_dir():
        raise HTTPException(status_code=404, detail="没装这个插件")
    with _LOCK:
        shutil.rmtree(dest, ignore_errors=True)
        enabled = _read_enabled()
        enabled.pop(name, None)
        _atomic_write(ENABLED_PATH, enabled)
    return {"ok": True, "state": "not_installed"}


def mounted(context: str = "chat") -> tuple[Optional[str], list[str]]:
    """启用中的合法插件 → (mcp-config 文件路径, 工具白名单)。没有则 (None, [])。
    config 文件现渲染进 state/（stdio：本仓 venv 的 python 起清单里的 entry）。

    context＝这次是给谁挂：'chat'（聊天，全挂）或 'wake'（醒来，摘掉 NO_WAKE_PLUGINS）。
    认不出的 context 一律按 chat 处理——多挂比少挂容易被发现，静默少挂会让人以为工具坏了。"""
    cfg_path = MCP_CONFIG_PATHS.get(context, MCP_CONFIG_PATHS["chat"])
    blocked = NO_WAKE_PLUGINS if context == "wake" else set()
    enabled = _read_enabled()
    servers: dict = {}
    tools: list[str] = []
    for name, on in sorted(enabled.items()):
        if not on or name in blocked:
            continue
        m = _read_manifest(name)
        if m is None:
            continue
        servers[name] = {"type": "stdio", "command": sys.executable,
                         "args": [str(PLUGINS_DIR / name / m["entry"])]}
        tools += [f"mcp__{name}__{t}" for t in m["tools"]]
    if not servers:
        return None, []
    payload = json.dumps({"mcpServers": servers}, ensure_ascii=False)
    if not cfg_path.exists() or cfg_path.read_text("utf-8") != payload:
        tmp = cfg_path.with_name(f".{cfg_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(cfg_path)
    return str(cfg_path), tools
