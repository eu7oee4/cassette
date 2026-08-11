"""拉取/更新 MaaYuan 任务资源（钉 commit）到 state/game/maayuan。

用法（在 server/ 的 venv 里跑）：
    python tools/fetch_maayuan.py            # 首次拉取或对齐到钉的 commit
    python tools/fetch_maayuan.py --check    # 只报告本地状态，不动

干四件事：clone/checkout 钉的 commit → 拉 MaaCommonAssets submodule（OCR 模型）→
跑 MaaYuan 自己的 configure_ocr_model()（识别 mac 用 ppocr_v4）→ 装 requirements-game.txt。

升钉流程（游戏改版、MaaYuan 出了新适配）：改这里的 PINNED_COMMIT → 重跑本脚本。
同插件 registry 的纪律：只认钉死的 commit，不追 main。
"""
import subprocess
import sys
from pathlib import Path

REPO_URL = "https://github.com/syoius/MaaYuan"
PINNED_COMMIT = "080fdf15f7441a81ac2fe9ad1981e5ba22889d46"   # 2026-08-11 验证过的版本

SERVER_DIR = Path(__file__).resolve().parent.parent
DEST = SERVER_DIR / "state" / "game" / "maayuan"
REQS = SERVER_DIR / "requirements-game.txt"


def run(*args: str, cwd: Path | None = None) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def current_commit() -> str:
    try:
        p = subprocess.run(["git", "rev-parse", "HEAD"], cwd=DEST,
                           capture_output=True, text=True, check=True)
        return p.stdout.strip()
    except Exception:
        return ""


def main() -> None:
    if "--check" in sys.argv:
        cur = current_commit()
        print(f"本地: {cur or '(未拉取)'}")
        print(f"钉的: {PINNED_COMMIT}")
        print("状态: " + ("一致" if cur == PINNED_COMMIT else "需要跑一遍本脚本"))
        return
    if not DEST.exists():
        DEST.parent.mkdir(parents=True, exist_ok=True)
        run("git", "clone", REPO_URL, str(DEST))
    if current_commit() != PINNED_COMMIT:
        run("git", "fetch", "origin", cwd=DEST)
        run("git", "checkout", "--detach", PINNED_COMMIT, cwd=DEST)
    run("git", "submodule", "update", "--init", "--depth", "1",
        "assets/MaaCommonAssets", cwd=DEST)
    # MaaYuan 自己的模型装配脚本（把 MaaCommonAssets 的 OCR 模型拷进 resource/*/model）
    sys.path.insert(0, str(DEST))
    from configure import configure_ocr_model   # noqa: E402
    configure_ocr_model()
    run(sys.executable, "-m", "pip", "install", "-q", "-r", str(REQS))
    print("完成。GAME_MODE_ENABLED=1 后重启后端即可用。")


if __name__ == "__main__":
    main()
