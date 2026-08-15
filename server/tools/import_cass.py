#!/usr/bin/env python3
"""Cass 搬家（PLAN_multichar M3 服务端部分）：mianmian → cassette 角色目录。

做四件事（默认只预览，--apply 才落盘；目标已存在时拒绝覆盖，--force 才覆盖）：
1. persona：1–96 行原样；「技术环境」里只保留 Ombre 记忆 + 时间感两小节，
   Playroom / 轮盘（Ruota della Fortuna）/ 团团三段删掉（mianmian 独有，
   cassette 的工具能力走 tool_menu / 插件 addendum，不写回 persona）。
   对预期章节有硬断言：mianmian persona 结构变了就报错，绝不静默裁错。
2. char.json：接线 18001（记忆一个字节不动，跟人走）。token/密码从
   ~/Documents/ombre-cass-run/.env 现读——本脚本进 git，密钥绝不写死在这里。
3. wake_log.jsonl：拷入 Cass 的 state 命名空间（Mind 页历史无缝续上）。
   字段差异：mianmian 记 beijing，cassette 同位字段叫 time——补 time、留 beijing。
4. settings.json：把 mianmian 的醒来设置（同一套 schema 的祖先）原样带过来，
   补 agent_name。聊天记录是 iOS 侧的事，另见 import_cass_chat.py。

用法：
    python3 tools/import_cass.py            # 预览（不写盘）
    python3 tools/import_cass.py --apply    # 正式执行
"""
import argparse
import json
import sys
from pathlib import Path

CHAR_ID = "cass"
DISPLAY_NAME = "Cassius"          # persona 第一句的名字；app 设置页可改（settings.agent_name 优先）
OMBRE_MCP_URL = "http://localhost:18001/mcp"

SERVER_DIR = Path(__file__).resolve().parent.parent          # ~/cassette/server
MIANMIAN_SERVER = Path.home() / "mianmian-app" / "server"
OMBRE_RUN_ENV = Path.home() / "Documents" / "ombre-cass-run" / ".env"

CHAR_DIR = SERVER_DIR / "characters" / CHAR_ID
STATE_DIR = SERVER_DIR / "state" / "characters" / CHAR_ID

# persona「技术环境」章节的处置表：这里没列的小节名 = 结构漂移，直接报错。
TECH_HEADER = "## 技术环境"
TECH_KEEP = ["### 记忆（Ombre）", "### 时间感"]
TECH_DROP = ["### Playroom（露骨细节的另一处记忆）", "### Ruota della Fortuna（轮盘）"]
TAIL_DROP = ["## 关于团团和它的工具"]     # 技术环境之后的整章删除


def die(msg: str) -> None:
    sys.exit(f"❌ {msg}")


def transform_persona(raw: str) -> str:
    """1–96 原样 + 技术环境白名单小节。按标题切块，硬断言预期结构。"""
    lines = raw.splitlines()
    try:
        tech_at = lines.index(TECH_HEADER)
    except ValueError:
        die(f"persona 里找不到「{TECH_HEADER}」——结构变了，先人工核对再改本脚本")

    head = lines[:tech_at]                       # 1–96 + 空行，原样保留

    # 技术环境之后按 ##/### 标题切块
    blocks: list[tuple[str, list[str]]] = []     # (标题行, 含标题的整块)
    cur_title, cur = TECH_HEADER, [TECH_HEADER]
    for ln in lines[tech_at + 1:]:
        if ln.startswith("## ") or ln.startswith("### "):
            blocks.append((cur_title, cur))
            cur_title, cur = ln, [ln]
        else:
            cur.append(ln)
    blocks.append((cur_title, cur))

    titles = [t for t, _ in blocks]
    for expected in TECH_KEEP + TECH_DROP + TAIL_DROP:
        if expected not in titles:
            die(f"persona 缺预期章节「{expected}」——结构变了，先人工核对再改本脚本")
    known = {TECH_HEADER, *TECH_KEEP, *TECH_DROP, *TAIL_DROP}
    strange = [t for t in titles if t not in known]
    if strange:
        die(f"persona 出现处置表之外的章节 {strange}——先决定去留再跑")

    kept = {t: b for t, b in blocks if t in TECH_KEEP}
    out = head + [TECH_HEADER, ""]
    for t in TECH_KEEP:                          # 按白名单顺序拼回，块间留一个空行
        body = kept[t]
        while body and body[-1] == "":
            body = body[:-1]
        out += body + [""]
    return "\n".join(out).rstrip() + "\n"


def read_ombre_secrets() -> tuple[str, str]:
    """从 ombre-cass-run/.env 现读 token/密码（那里是运行时的唯一权威）。"""
    if not OMBRE_RUN_ENV.exists():
        die(f"找不到 {OMBRE_RUN_ENV}（18001 的运行配置），无法接线")
    kv = {}
    for ln in OMBRE_RUN_ENV.read_text("utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            kv[k.strip()] = v.strip()
    token = kv.get("OMBRE_MCP_TOKEN", "")
    pwd = kv.get("OMBRE_DASHBOARD_PASSWORD", "")
    if not token or not pwd:
        die("ombre-cass-run/.env 里缺 OMBRE_MCP_TOKEN 或 OMBRE_DASHBOARD_PASSWORD")
    return token, pwd


def transform_wake_log(raw: str) -> tuple[str, int]:
    """逐行补 time 字段（mianmian 叫 beijing）。坏行保留原样不丢（宁可脏别丢历史）。"""
    out, n = [], 0
    for ln in raw.splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
            if not obj.get("time") and obj.get("beijing"):
                obj["time"] = obj["beijing"]
            out.append(json.dumps(obj, ensure_ascii=False))
        except json.JSONDecodeError:
            out.append(ln)
        n += 1
    return "\n".join(out) + "\n", n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真的写盘（缺省只预览）")
    ap.add_argument("--force", action="store_true", help="目标已存在也覆盖")
    args = ap.parse_args()

    # ---- 读源 ----
    src_persona = MIANMIAN_SERVER / "persona.md"
    src_wake = MIANMIAN_SERVER / "state" / "wake_log.jsonl"
    src_settings = MIANMIAN_SERVER / "state" / "settings.json"
    for p in (src_persona, src_wake, src_settings):
        if not p.exists():
            die(f"源文件不存在：{p}")

    persona = transform_persona(src_persona.read_text("utf-8"))
    wake_log, wake_n = transform_wake_log(src_wake.read_text("utf-8"))
    token, pwd = read_ombre_secrets()

    settings = json.loads(src_settings.read_text("utf-8"))
    settings["agent_name"] = DISPLAY_NAME
    # 落地即休眠：mianmian 侧退役前两边不能同时让 Cass 醒来（plan 第一条：不双活）。
    # 切换日 unload mianmian 后端之后，再在 app 设置页（或手编本文件）把 enabled 打开。
    settings["enabled"] = False

    char_json = {
        "display_name": DISPLAY_NAME,
        "engine": "claude-code",
        "ombre": {"mcp_url": OMBRE_MCP_URL, "mcp_token": token,
                  "dashboard_password": pwd},
    }

    targets = {
        CHAR_DIR / "persona.md": persona,
        CHAR_DIR / "char.json": json.dumps(char_json, ensure_ascii=False, indent=2) + "\n",
        STATE_DIR / "wake_log.jsonl": wake_log,
        STATE_DIR / "settings.json": json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
    }

    # ---- 预览 ----
    print(f"persona: {len(persona.splitlines())} 行（源 {len(src_persona.read_text('utf-8').splitlines())} 行）")
    print(f"wake_log: {wake_n} 条")
    print(f"char.json: mcp_url={OMBRE_MCP_URL}  token/密码 ← {OMBRE_RUN_ENV}")
    print(f"settings: {json.dumps(settings, ensure_ascii=False)}")
    for p in targets:
        mark = "⚠️ 已存在" if p.exists() else "新建"
        print(f"  → {p}  [{mark}]")

    if not args.apply:
        print("\n（预览模式，没写盘。--apply 执行）")
        return
    exists = [p for p in targets if p.exists()]
    if exists and not args.force:
        die(f"目标已存在：{exists}。确认要覆盖用 --force")

    for p, content in targets.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        print(f"✅ 写入 {p}")
    print(f"\n完成。角色 id={CHAR_ID}。后端热读 characters/，新角色下次请求即生效；"
          f"验证：GET /mind?char={CHAR_ID}、app 会话列表应出现 {DISPLAY_NAME}。")


if __name__ == "__main__":
    main()
