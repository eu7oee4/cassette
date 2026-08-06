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
MCP_CONFIG_PATH = state_store.STATE_DIR / "plugins.mcp.json"

# 写死的插件 registry：只认自己名下的仓。上新插件 = 改这里 + 发版，不做远程 registry。
REGISTRY: dict[str, dict] = {
    "webpage": {
        "repo": "https://github.com/eu7oee4/cassette-plugin-webpage",
        "display_name": "网页工坊",
        "description": "做 / 改 / 传送 HTML 网页（第一个插件）",
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


def install(name: str) -> dict:
    """从 registry 白名单 clone 插件仓。装完默认「已装未启用」，启用是用户的显式动作。"""
    _check_name(name)
    reg = REGISTRY.get(name)
    if reg is None:
        raise HTTPException(status_code=403, detail="不在插件白名单里（registry 写死，只认自己名下的仓）")
    dest = PLUGINS_DIR / name
    with _LOCK:
        if dest.exists():
            raise HTTPException(status_code=409, detail="已安装过；要更新先卸载再装")
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["git", "clone", "--depth", "1", reg["repo"], str(dest)],
                           capture_output=True, text=True, timeout=180, check=True)
        except subprocess.TimeoutExpired:
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(status_code=504, detail="clone 超时（网络问题？）")
        except subprocess.CalledProcessError as e:
            shutil.rmtree(dest, ignore_errors=True)
            raise HTTPException(status_code=502, detail=f"clone 失败：{(e.stderr or '')[:200]}")
    if _read_manifest(name) is None:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=502, detail="插件清单（plugin.json）不合法，已回滚")
    return {"ok": True, "state": "disabled"}


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


def mounted() -> tuple[Optional[str], list[str]]:
    """启用中的合法插件 → (mcp-config 文件路径, 工具白名单)。没有则 (None, [])。
    config 文件现渲染进 state/（stdio：本仓 venv 的 python 起清单里的 entry）。"""
    enabled = _read_enabled()
    servers: dict = {}
    tools: list[str] = []
    for name, on in sorted(enabled.items()):
        if not on:
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
    if not MCP_CONFIG_PATH.exists() or MCP_CONFIG_PATH.read_text("utf-8") != payload:
        _atomic_write_raw = MCP_CONFIG_PATH.with_name(
            f".{MCP_CONFIG_PATH.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        _atomic_write_raw.write_text(payload, "utf-8")
        _atomic_write_raw.replace(MCP_CONFIG_PATH)
    return str(MCP_CONFIG_PATH), tools
