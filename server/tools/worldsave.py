"""世界存档（2026-08-19）：把「现在的小屋」整个拓一份下来，回头能整个倒回去。

为什么要有：左滑删除只能一轮一轮往回抠，而**改地点、搬房间、批量清事件**这类动作
是抠不回来的——registry.json 的地点状态快照没有历史，改完旧文本就没了。所以在动
之前先存一档，后悔了整体回滚。

存的是「AI 会读到的那些文件」的全集，也就是注入的全部来源：
    world.json                       谁在哪
    rooms/                           房间注册表（含地点状态快照）+ 每个房间的事件流
    characters/<角色>/experience.jsonl   第一人称经历流（房间里看见的）
    characters/<角色>/wake_log.jsonl     醒来时的内心（也进注入时间线）
    characters/<角色>/recent_window.json 最近对话窗口
    characters/<角色>/schedule.json      下次醒来 / 错误冷却
    pets/                            团团的状态 + 照料记录

**不存**：手机聊天记录（在 TA 手机上，不在这儿）、邮件、插件配置、code/game/浏览器
的运行时（几百 MB 且与小屋无关）、persona 和角色设定（那些在仓库里，git 管着）。

存档放 state/snapshots/<时间戳>-<名字>/，state/ 本来就 gitignore，不会进仓库。
一份存档几 MB 量级，随便存。

用法（cwd = server/）：
    .venv/bin/python tools/worldsave.py save 动地点之前
    .venv/bin/python tools/worldsave.py list
    .venv/bin/python tools/worldsave.py restore 动地点之前     # 前缀/片段能对上就行
    .venv/bin/python tools/worldsave.py restore latest

回档做三件事，顺序讲究：
1. **先把队列按暂停**（HTTP 打后端的 /world/pause）——不然回档写到一半，某个角色
   正好醒来往 events.jsonl 里追一行，回出来的世界是半新半旧的；
2. 把当前状态自动存一档（auto-回档前）——回档本身也可回档，不存在「一步走错全没了」；
3. 覆盖文件。回完队列**保持暂停**，让机主自己看一眼再按「开始」。
后端没起也能回（第 1 步跳过，打印一句提醒）——文件层的事不依赖服务活着。
"""
import argparse
import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config          # noqa: E402
import state_store     # noqa: E402

STATE = state_store.STATE_DIR
SNAP_ROOT = STATE / "snapshots"
BACKEND = "http://127.0.0.1:8000"

WORLD_FILES = ["world.json"]
WHOLE_DIRS = ["rooms", "pets"]
CHAR_FILES = ["experience.jsonl", "wake_log.jsonl", "recent_window.json", "schedule.json"]


# ---------- 工具 ----------
def _slug(name: str) -> str:
    """名字进路径：空白转横杠，去掉分隔符，别的（中文随便写）原样留着。"""
    s = re.sub(r"\s+", "-", (name or "").strip())
    return re.sub(r"[/\\:]", "", s)[:40]


def _pause(on: bool) -> bool:
    """按暂停/开始。后端没起或没配 AUTH_KEY → False（调用方打印提醒，不当错误）。"""
    if not config.AUTH_KEY:
        return False
    req = urllib.request.Request(
        f"{BACKEND}/world/pause", data=json.dumps({"on": on}).encode(), method="POST",
        headers={"X-Auth": config.AUTH_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def _replying() -> list:
    """现在有谁正在生成醒来回应（回档前的最后一道人工确认用）。"""
    if not config.AUTH_KEY:
        return []
    req = urllib.request.Request(f"{BACKEND}/world", headers={"X-Auth": config.AUTH_KEY})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []
    return [e for e, v in (data.get("entities") or {}).items() if v.get("status")]


def _du(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _char_dirs(root: Path) -> list:
    if not root.exists():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)


def _copy_into(src: Path, dst: Path) -> int:
    """存档方向的拷贝：文件照拷、目录整棵拷。返回拷了多少字节（不存在算 0）。"""
    if not src.exists():
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)
    return _du(dst)


# ---------- save ----------
def save(name: str, kind: str = "manual") -> Path:
    stamp = time.strftime("%m%d-%H%M%S")
    out = SNAP_ROOT / f"{stamp}-{_slug(name) or kind}"
    out.mkdir(parents=True, exist_ok=True)

    total = 0
    for f in WORLD_FILES:
        total += _copy_into(STATE / f, out / f)
    for d in WHOLE_DIRS:
        total += _copy_into(STATE / d, out / d)
    chars = []
    for cdir in _char_dirs(state_store.CHAR_STATE_ROOT):
        got = 0
        for f in CHAR_FILES:
            if (cdir / f).exists():
                total += _copy_into(cdir / f, out / "characters" / cdir.name / f)
                got += 1
        if got:
            chars.append(cdir.name)

    (out / "manifest.json").write_text(json.dumps({
        "name": name, "kind": kind, "created_at": int(time.time()),
        "created_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chars": chars, "bytes": total,
    }, ensure_ascii=False, indent=2), "utf-8")
    print(f"存好了：{out.name}（{total // 1024} KB，角色 {'、'.join(chars) or '无'}）")
    print(f"   路径 {out}")
    return out


# ---------- list ----------
def _snapshots() -> list:
    if not SNAP_ROOT.exists():
        return []
    return sorted((p for p in SNAP_ROOT.iterdir() if (p / "manifest.json").exists()),
                  key=lambda p: p.name)


def show_list() -> None:
    snaps = _snapshots()
    if not snaps:
        print("还没有存档。先 save 一个。")
        return
    print(f"{len(snaps)} 份存档（新的在下面）：")
    for p in snaps:
        m = json.loads((p / "manifest.json").read_text("utf-8"))
        tag = "自动" if m.get("kind") != "manual" else "手存"
        print(f"  {p.name:<28} {tag}  {m.get('created_str', '')}  "
              f"{m.get('bytes', 0) // 1024} KB  {m.get('name', '')}")


def _kind(p: Path) -> str:
    try:
        return json.loads((p / "manifest.json").read_text("utf-8")).get("kind", "manual")
    except Exception:
        return "manual"


def _resolve(key: str) -> Path:
    """片段匹配，但**手存的优先**。原因：回档会自动存一份叫「回档前-<原档名>」的自动档，
    它的名字天然包含原档名且更新——不分手存/自动的话，第二次 restore 同一个名字会回到
    那份「回档前（也就是坏掉的）」快照上，一脚踩空。要回自动档就写全名或写「回档前」。"""
    snaps = _snapshots()
    if not snaps:
        raise SystemExit("没有任何存档")
    if key in ("latest", "last", "最新"):
        return snaps[-1]
    exact = [p for p in snaps if p.name == key]
    if exact:
        return exact[0]
    manual = [p for p in snaps if key in p.name and _kind(p) == "manual"]
    autos = [p for p in snaps if key in p.name and _kind(p) != "manual"]
    hit = manual or autos
    if not hit:
        raise SystemExit(f"没有对上的存档：{key}（先跑 list 看看）")
    if len(hit) > 1:
        print(f"对上 {len(hit)} 份，取最新那份：{hit[-1].name}")
    return hit[-1]


# ---------- restore ----------
def restore(key: str, yes: bool = False) -> None:
    snap = _resolve(key)
    m = json.loads((snap / "manifest.json").read_text("utf-8"))
    print(f"要回到：{snap.name}（{m.get('created_str')}，{m.get('name')}）")

    gen = _replying()
    if gen and not yes:
        raise SystemExit(f"现在 {'、'.join(gen)} 正在生成回应——等它写完再回档，"
                         f"或者加 --yes 强行来（可能回出半新半旧的事件流）")
    if not yes:
        ans = input("回档会覆盖现在的世界（当前状态会先自动存一份）。确定？[y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            raise SystemExit("没动。")

    paused = _pause(True)
    print("队列已暂停（回完保持暂停，你看一眼再按开始）" if paused
          else "⚠️ 没能按到暂停键（后端没起？）——确认没有醒来正在写盘再继续")
    if paused:
        time.sleep(1.5)   # 让正在落笔的那一下写完

    save(f"回档前-{snap.name}", kind="auto")

    for f in WORLD_FILES:
        if (snap / f).exists():
            shutil.copy2(snap / f, STATE / f)
    for d in WHOLE_DIRS:
        if (snap / d).exists():
            # 整棵换掉：存档之后新建的房间/新落的事件文件也要跟着消失，不然半新半旧
            shutil.rmtree(STATE / d, ignore_errors=True)
            shutil.copytree(snap / d, STATE / d)
    # 角色的四个文件按名字覆盖回去。存档里没有的角色（这之后才建的）原样留着不动——
    # 删一个角色的记忆比留着更难收场，宁可留。
    for cdir in _char_dirs(snap / "characters"):
        for f in CHAR_FILES:
            if (cdir / f).exists():
                dst = state_store.CHAR_STATE_ROOT / cdir.name / f
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cdir / f, dst)

    print(f"回好了：世界已经是 {m.get('created_str')} 那会儿的样子。")
    print("队列还停着——在 app 里看一眼，觉得对了再按「开始」。"
          if paused else "记得确认队列状态。")


def main() -> None:
    ap = argparse.ArgumentParser(description="小屋世界存档 / 回档")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save", help="存一档")
    s.add_argument("name", nargs="?", default="手存", help="给这一档起个名（可中文）")
    sub.add_parser("list", help="看有哪些存档")
    r = sub.add_parser("restore", help="回到某一档")
    r.add_argument("key", help="存档名的任意片段 / 完整目录名 / latest")
    r.add_argument("--yes", action="store_true", help="不问直接回（脚本里用）")
    a = ap.parse_args()
    if a.cmd == "save":
        save(a.name)
    elif a.cmd == "list":
        show_list()
    else:
        restore(a.key, yes=a.yes)


if __name__ == "__main__":
    main()
