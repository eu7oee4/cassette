"""把一份 Markdown 渲成 PDF（小红书附件用）。

用法：
    server/.venv/bin/python server/tools/md_to_pdf.py <文件.md> [-o 输出.pdf] [--keep]

零依赖：自己把 md 转成 HTML，再用 Chrome 的 --print-to-pdf 打印。
Chromium 蹭 browser 插件那只（state/browser-runtime），跟渲卡片、渲简历同一条路——
不往 venv 里装 markdown/weasyprint 之类的东西。

只认这份手记实际用到的语法：# ## ###、--- 分隔线、``` 代码块、> 引用、
表格、有序/无序列表（含缩进续行）、**粗体**、`行内代码`，
外加两个自己的扩展：独占一行的 `<!-- 分页 -->` = 强制换页，
`<!-- 贴底 -->` = 之后的内容贴在末页页面底部（要先有一次分页）。
没实现的（图片、链接、嵌套列表、脚注）遇到了会原样漏出来——要用之前先看一眼输出。
"""
from __future__ import annotations

import argparse
import html as ht
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CSS = """
:root{
  --bg:#FFFFFF; --ink:#1C1A17; --muted:#6F6659; --accent:#C4552F;
  --line:#E2D9CB; --panel:#F7F3EC;
}
@page{ size:A4; margin:18mm 16mm 16mm; }
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:"PingFang SC","Hiragino Sans GB",system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
  color:var(--ink); background:var(--bg);
  font-size:10.5pt; line-height:1.85;
}
h1{font-size:20pt;line-height:1.35;font-weight:800;letter-spacing:-.01em;margin-bottom:14pt}
h2{font-size:14.5pt;line-height:1.4;font-weight:800;margin:22pt 0 9pt;
   padding-top:9pt;border-top:2px solid var(--line);page-break-after:avoid}
h3{font-size:11.5pt;line-height:1.5;font-weight:800;color:var(--accent);
   margin:14pt 0 6pt;page-break-after:avoid}
p{margin:0 0 8pt}
strong,b{font-weight:800}
em{font-style:normal;color:var(--accent);font-weight:800}
code{font-family:"SF Mono",ui-monospace,Menlo,monospace;font-size:9pt;
     background:var(--panel);border:1px solid var(--line);border-radius:4px;
     padding:1px 4px;color:#4A4238}
pre{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:10pt 12pt;margin:0 0 10pt;overflow:hidden;page-break-inside:avoid}
pre code{font-size:9pt;line-height:1.7;background:none;border:none;padding:0;
         white-space:pre-wrap;word-break:break-word;display:block}
blockquote{border-left:4px solid var(--accent);background:var(--panel);
  border-radius:0 8px 8px 0;padding:9pt 13pt;margin:0 0 10pt;color:#3A342C}
blockquote p:last-child{margin-bottom:0}
ul,ol{margin:0 0 10pt;padding-left:0;list-style:none}
li{position:relative;padding-left:19px;margin-bottom:5pt}
ul>li::before{content:"";position:absolute;left:0;top:.72em;
  width:9px;height:3px;background:var(--accent);border-radius:2px}
ol{counter-reset:li}
ol>li{counter-increment:li}
ol>li::before{content:counter(li) ".";position:absolute;left:0;top:0;
  color:var(--accent);font-weight:800}
table{width:100%;border-collapse:collapse;margin:0 0 11pt;font-size:9pt;
      line-height:1.55;page-break-inside:avoid}
th,td{border:1px solid var(--line);padding:6pt 8pt;text-align:left;vertical-align:top}
th{background:var(--panel);font-weight:800;color:var(--muted)}
td code,th code{font-size:8.2pt}
hr{border:none;border-top:2px solid var(--line);margin:16pt 0}
.pagebreak{page-break-before:always;height:0}
/* 分页后紧跟的 h2 不用再顶一条横线和一大截上留白 */
.pagebreak + h2, .pagebox > h2:first-child{margin-top:0;padding-top:0;border-top:none}
/* 末页整页高的盒子：<!-- 贴底 --> 之后的内容被推到页面底部 */
.pagebox{height:262mm;display:flex;flex-direction:column}
.pagebox > .tail{margin-top:auto}
.doc-foot{margin-top:20pt;padding-top:8pt;border-top:2px solid var(--line);
  font-size:8.5pt;letter-spacing:.06em;color:var(--muted);font-weight:600}
"""

FOOT = "跑在 Mac 上的 Claude 伴侣 · eu7oee4 · cassette"
BOTTOM_MARK = "<!--BOTTOM-->"


def inline(s: str) -> str:
    """行内标记。先抽走 `code`，免得代码里的星号被当粗体。"""
    stash: list[str] = []

    def keep(m: re.Match) -> str:
        stash.append(f"<code>{ht.escape(m.group(1))}</code>")
        return f"\x00{len(stash) - 1}\x00"

    s = re.sub(r"`([^`]+)`", keep, s)
    s = ht.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], s)


CJK = re.compile(r"[\u2E80-\u9FFF\uFF00-\uFFEF\u3000-\u303F]")


def soft_join(parts: list[str]) -> str:
    """软换行接回一行：中文之间不要塞空格（md 的换行在中文里不是空格）。"""
    s = parts[0].strip()
    for nxt in parts[1:]:
        nxt = nxt.strip()
        if not nxt:
            continue
        sep = "" if (s and CJK.match(s[-1]) and CJK.match(nxt[0])) else " "
        s += sep + nxt
    return s


def _row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def to_html(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        ln = lines[i]
        st = ln.strip()

        if not st:
            i += 1
            continue

        # 代码块
        if st.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{ht.escape(chr(10).join(buf))}</code></pre>")
            continue

        # 手动分页：md 里独占一行的 <!-- 分页 -->
        if st in ("<!-- 分页 -->", "<!-- pagebreak -->"):
            out.append('<div class="pagebreak"></div>')
            i += 1
            continue

        # 贴底：这一行之后的内容推到末页页面底部（配合上面的强制分页用）
        if st in ("<!-- 贴底 -->", "<!-- bottom -->"):
            out.append(BOTTOM_MARK)
            i += 1
            continue

        # 分隔线（要在表格/标题之后判断，--- 只在独占一行时算）
        if re.fullmatch(r"(-{3,}|\*{3,})", st):
            out.append("<hr>")
            i += 1
            continue

        # 标题
        m = re.match(r"(#{1,4})\s+(.*)", st)
        if m:
            lv = len(m.group(1))
            out.append(f"<h{lv}>{inline(m.group(2))}</h{lv}>")
            i += 1
            continue

        # 表格：本行有 |，下一行是 |---|
        if st.startswith("|") and i + 1 < n and re.fullmatch(r"\|[\s:|-]+\|", lines[i + 1].strip()):
            head = _row(st)
            i += 2
            body = []
            while i < n and lines[i].strip().startswith("|"):
                body.append(_row(lines[i].strip()))
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            trs = "".join(
                "<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body
            )
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
            continue

        # 引用块（连续的 > 行合成一段，空的 > 行 = 段落分隔）
        if st.startswith(">"):
            buf: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            paras, cur = [], []
            for b in buf:
                if b.strip():
                    cur.append(b.strip())
                elif cur:
                    paras.append(cur)
                    cur = []
            if cur:
                paras.append(cur)
            # 先用哨兵拼成一段再解析行内标记——**粗体**可能跨行，逐行解析会拆散它
            inner = "".join(
                "<p>" + inline("\x01".join(pa)).replace("\x01", "<br>") + "</p>"
                for pa in paras
            )
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # 列表（缩进续行并进同一个 li）
        m = re.match(r"([-*+]|\d+\.)\s+(.*)", st)
        if m:
            tag = "ul" if m.group(1) in "-*+" else "ol"
            items: list[str] = []
            while i < n:
                cur = lines[i]
                s2 = cur.strip()
                mm = re.match(r"([-*+]|\d+\.)\s+(.*)", s2)
                if mm and not cur.startswith("    "):
                    items.append(mm.group(2))
                    i += 1
                elif s2 and cur.startswith("  ") and items:
                    items[-1] = soft_join([items[-1], s2])   # 续行
                    i += 1
                else:
                    break
            lis = "".join(f"<li>{inline(x)}</li>" for x in items)
            out.append(f"<{tag}>{lis}</{tag}>")
            continue

        # 段落（软换行按空格接上，中文不需要 <br>）
        buf = [st]
        i += 1
        while i < n:
            s2 = lines[i].strip()
            if not s2 or re.match(r"(#{1,4}\s|```|>|\||[-*+]\s|\d+\.\s)", s2) \
                    or re.fullmatch(r"(-{3,}|\*{3,})", s2):
                break
            buf.append(s2)
            i += 1
        out.append(f"<p>{inline(soft_join(buf))}</p>")

    return "\n".join(out)


def pin_bottom(body: str) -> str:
    """有 <!-- 贴底 --> 的话，把最后一次分页之后的内容包成一个整页高的盒子，
    标记之后的部分（连同文末署名）用 margin-top:auto 顶到页面底部。"""
    if BOTTOM_MARK not in body:
        return body
    head, _, rest = body.rpartition('<div class="pagebreak"></div>')
    if BOTTOM_MARK not in rest:            # 标记跑到最后一次分页之前了，贴底无从谈起
        return body.replace(BOTTOM_MARK, "")
    top, _, tail = rest.partition(BOTTOM_MARK)
    return (f'{head}<div class="pagebreak"></div>'
            f'<div class="pagebox">{top}<div class="tail">{tail}</div></div>')


def find_chrome() -> str | None:
    browsers = ROOT / "state" / "browser-runtime" / "browsers"
    for app in sorted(browsers.glob("chromium-*/chrome-mac-*/Google Chrome for Testing.app")):
        binp = app / "Contents" / "MacOS" / "Google Chrome for Testing"
        if binp.exists():
            return str(binp)
    sys_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return str(sys_chrome) if sys_chrome.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("md")
    ap.add_argument("-o", "--out", help="输出 PDF（默认同名同目录）")
    ap.add_argument("--keep", action="store_true", help="留下中间 html，方便调样式")
    a = ap.parse_args()

    src = Path(a.md).resolve()
    if not src.exists():
        print(f"找不到 {src}", file=sys.stderr)
        return 1
    chrome = find_chrome()
    if not chrome:
        print("找不到 Chromium（装 browser 插件会带一只）", file=sys.stderr)
        return 1

    body = to_html(src.read_text("utf-8")) + f'<div class="doc-foot">{FOOT}</div>'
    body = pin_bottom(body)
    page = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f"<style>{CSS}</style></head><body>{body}</body></html>")
    tmp = src.with_suffix(".render.html")
    tmp.write_text(page, "utf-8")

    out = Path(a.out).resolve() if a.out else src.with_suffix(".pdf")
    proc = subprocess.run(
        [chrome, "--headless", "--disable-gpu", "--no-first-run", "--no-pdf-header-footer",
         f"--print-to-pdf={out}", f"file://{tmp}"],
        capture_output=True, timeout=120)
    if not out.exists():
        print(f"渲染失败: {proc.stderr.decode()[-400:]}", file=sys.stderr)
        return 1
    if not a.keep:
        tmp.unlink()
    print(f"{out}  {out.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
