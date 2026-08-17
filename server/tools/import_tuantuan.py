"""团团搬家脚本（PLAN_pet P1，搬家不分身）。

从 mianmian-app 把团团的实例数据迁进 cassette：
    mianmian/server/prompts/pet_persona.md  → server/pets/tuantuan/persona.md
    （新建）                                 → server/pets/tuantuan/pet.json
    mianmian/server/state/pet/state.json    → server/state/pets/tuantuan/state.json
                                              （poke 摘除、补 poop_pressure；
                                               created_at 保留——养到第几天不清零）
    mianmian/server/state/pet/petlog.jsonl  → server/state/pets/tuantuan/petlog.jsonl
    mianmian/server/.env 的 DEEPSEEK_*      → server/.env（缺才补，不动已有值）

跑法（cwd = server/）：
    .venv/bin/python tools/import_tuantuan.py [mianmian_server_dir] [--force]

已存在的目标文件一律不碰（--force 才覆盖）——搬完记得：
① 重启 cassette 后端（猫房补种 + 团团入世界）；
② mianmian 侧退役（P5：poke/overlay/pet MCP 摘除），**别双跑**——同一只猫
   两边喂就是状态分裂，搬家的意义就没了。
"""
import json
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PID = "tuantuan"


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--force"]
    force = "--force" in sys.argv
    src = Path(args[0]) if args else Path.home() / "mianmian-app" / "server"
    if not (src / "state" / "pet" / "state.json").exists():
        sys.exit(f"找不到 mianmian 的团团状态：{src}/state/pet/state.json")

    pet_dir = BASE / "pets" / PID
    state_dir = BASE / "state" / "pets" / PID
    done, skipped = [], []

    def put(path: Path, writer) -> None:
        if path.exists() and not force:
            skipped.append(str(path))
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        writer(path)
        done.append(str(path))

    # ① 注册表实例：pet.json + persona
    put(pet_dir / "pet.json", lambda p: p.write_text(json.dumps(
        {"display_name": "团团",
         "engine": {"api_key_env": "DEEPSEEK_API_KEY"}},
        ensure_ascii=False, indent=2), "utf-8"))
    persona_src = src / "prompts" / "pet_persona.md"
    if persona_src.exists():
        put(pet_dir / "persona.md", lambda p: shutil.copyfile(persona_src, p))

    # ② 状态：poke 摘除、补 poop_pressure/pose；created_at 保留
    def write_state(p: Path) -> None:
        s = json.loads((src / "state" / "pet" / "state.json").read_text("utf-8"))
        s.pop("poke", None)
        s.setdefault("poop_pressure", 0.0)
        s.setdefault("pose", None)
        p.write_text(json.dumps(s, ensure_ascii=False, indent=2), "utf-8")
    put(state_dir / "state.json", write_state)

    # ③ petlog 原样搬（照料史 = 团团的记忆）
    log_src = src / "state" / "pet" / "petlog.jsonl"
    if log_src.exists():
        put(state_dir / "petlog.jsonl", lambda p: shutil.copyfile(log_src, p))

    # ④ .env：DEEPSEEK_* 缺才补（不动已有值，不打印内容）
    env_path = BASE / ".env"
    have = env_path.read_text("utf-8") if env_path.exists() else ""
    add = []
    for ln in (src / ".env").read_text("utf-8").splitlines() \
            if (src / ".env").exists() else []:
        k = ln.split("=", 1)[0].strip()
        if k.startswith("DEEPSEEK") and f"{k}=" not in have:
            add.append(ln)
    if add:
        with env_path.open("a", encoding="utf-8") as f:
            f.write("\n# 团团引擎（import_tuantuan 搬入）\n" + "\n".join(add) + "\n")
        done.append(f"{env_path}（补 {len(add)} 个 DEEPSEEK 键）")

    for p in done:
        print(f"✓ {p}")
    for p in skipped:
        print(f"– 跳过（已存在，--force 才覆盖）：{p}")
    print("\n搬完两件事别忘：① 重启后端（团团入世界）；"
          "② mianmian 侧退役（P5）——别双跑，同一只猫两边喂就是状态分裂。")


if __name__ == "__main__":
    main()
