"""把一份 cards.html 渲成小红书规格的图片（3:4，1080×1440 CSS px @2x = 2160×2880）。

用法：
    server/.venv/bin/python server/tools/render_xhs.py <story_dir> [--scale 2] [--keep]

<story_dir> 里要有 `cards.html`：一个 <style> + 若干 <section class="card">，
每个 section 就是一张图。输出落 <story_dir>/cards/01.png … 序号即发布顺序。

为什么一张一个文件而不是整页截图：headless 的整页截图对固定高度的多屏排版不稳，
拆开截每张都是确定性的 viewport 快照（同入同出，重跑不会漂）。
Chromium 蹭 browser 插件那只（state/browser-runtime），跟简历渲 PDF 同一条路。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARD_W, CARD_H = 1080, 1440   # 3:4，小红书首图比例


def find_chrome() -> str | None:
    browsers = ROOT / "state" / "browser-runtime" / "browsers"
    for app in sorted(browsers.glob("chromium-*/chrome-mac-*/Google Chrome for Testing.app")):
        binp = app / "Contents" / "MacOS" / "Google Chrome for Testing"
        if binp.exists():
            return str(binp)
    sys_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(sys_chrome) if sys_chrome.exists() else None


def split_cards(html: str) -> tuple[str, list[str]]:
    """返回 (head 里的 <style>…</style> 原文, 每张卡的 section 原文列表)。"""
    style = "".join(re.findall(r"<style>.*?</style>", html, re.S))
    # class="card dark" 这类附加类必须一起匹配——写死 class="card" 会静默漏掉整张卡
    # （2026-08-29 踩过：12 张只渲出 9 张，封面和尾卡无声消失）。
    cards = re.findall(r'<section class="card[^"]*".*?</section>', html, re.S)
    total = html.count("<section")
    if total != len(cards):
        raise SystemExit(f"cards.html 里有 {total} 个 <section>，只认出 {len(cards)} 张卡"
                         "——漏掉的多半是 class 写法没对上，别让它静默少发几张")
    return style, cards


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("story_dir")
    ap.add_argument("--scale", type=float, default=2.0, help="设备像素比（默认 2 = 2160×2880）")
    ap.add_argument("--keep", action="store_true", help="留下拆出来的单张 html")
    a = ap.parse_args()

    d = Path(a.story_dir).resolve()
    src = d / "cards.html"
    if not src.exists():
        print(f"找不到 {src}", file=sys.stderr)
        return 1
    chrome = find_chrome()
    if not chrome:
        print("找不到 Chromium（装 browser 插件会带一只）", file=sys.stderr)
        return 1

    style, cards = split_cards(src.read_text("utf-8"))
    if not cards:
        print("cards.html 里没有 <section class=\"card\">", file=sys.stderr)
        return 1

    out = d / "cards"
    out.mkdir(exist_ok=True)
    for old in out.glob("*.png"):      # 重渲 = 整批覆盖，别留上一版的残页
        old.unlink()

    tmpdir = d / ".render-tmp"
    tmpdir.mkdir(exist_ok=True)
    made = []
    for i, card in enumerate(cards, 1):
        page = (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{style}'
                f'</head><body>{card}</body></html>')
        p = tmpdir / f"{i:02d}.html"
        p.write_text(page, "utf-8")
        png = out / f"{i:02d}.png"
        proc = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
             f"--force-device-scale-factor={a.scale}",
             f"--window-size={CARD_W},{CARD_H}",
             f"--screenshot={png}", f"file://{p}"],
            capture_output=True, timeout=90)
        if not png.exists():
            print(f"第 {i} 张渲染失败: {proc.stderr.decode()[-300:]}", file=sys.stderr)
            return 1
        made.append(png)
        print(f"  {png.name}  {png.stat().st_size // 1024} KB")

    if not a.keep:
        for p in tmpdir.glob("*.html"):
            p.unlink()
        tmpdir.rmdir()
    print(f"\n{len(made)} 张 → {out}（{int(CARD_W*a.scale)}×{int(CARD_H*a.scale)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
