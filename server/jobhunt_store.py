"""Jobhunt 存储（PLAN_jobhunt.md）——求职流水线的数据层，从 mianmian 搬来。

分块：需求档案(profile) + 简历仓库(md→HTML→headless Chrome 渲 PDF) + 岗位库(jds)
+ 投递台账(applications)。数据全在 state/jobhunt/（gitignore，简历=全套真实个人信息），
**全局一份不分角色**（拍板 2：双角色共挂同一份库）——防岔子靠结构：

- 数据层：所有工具调用经 MCP 壳 HTTP 转发到唯一后端进程，jsonl 在这里带 _LOCK 串行写，
  两个 claude 进程同时动手也坏不了数据。J3 的 mail watcher 也在同一进程里。
- 语义层：JD 状态机 new → scored → drafted → sent → archived(不投)，迁移在 jd_score /
  _jd_set_status 里原子执行；已打分的岗自然不在 status=new 的收件箱里，同瞬撞车的
  后到方收到「已被谁打过分」的工具结果。简历变体 id 重名直接拒（要覆盖需 overwrite）。
  台账/JD 带 char_id（经手人）。
- 出手层：发送不在工具面上。**mianmian 的 outbox/SMTP 整段退役**——email_draft 落
  邮箱插件的草稿信箱（mail_bridge，J2），机主在 app 点发送才出手；发出后 mail_bridge
  回调这里的 application_add + jd → sent。

渲染链（md_to_html + 模板 + Chrome 参数）是 mianmian 三个 commit 踩出来的，原样搬，
别顺手简化。Chrome 用 browser 插件装的 Playwright Chromium（零新依赖）。
"""
import json
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
import state_store

_LOCK = threading.Lock()


# ---------- 路径（全局一份；测试用 _rebase 指到临时目录）----------

def _rebase(job_dir: Path) -> None:
    """把全部路径钉到 job_dir 下并建目录。import 时钉一次真路径；单测钉临时目录。"""
    global JOB_DIR, RESUME_DIR, VARIANT_DIR, PDF_DIR
    global PROFILE_PATH, MASTER_PATH, TEMPLATE_PATH, JDS_PATH, APPS_PATH
    JOB_DIR = job_dir
    RESUME_DIR = JOB_DIR / "resume"
    VARIANT_DIR = RESUME_DIR / "variants"
    PDF_DIR = JOB_DIR / "pdf"
    PROFILE_PATH = JOB_DIR / "profile.md"
    MASTER_PATH = RESUME_DIR / "master.md"
    TEMPLATE_PATH = RESUME_DIR / "template.html"
    JDS_PATH = JOB_DIR / "jds.jsonl"
    APPS_PATH = JOB_DIR / "applications.jsonl"
    for d in (JOB_DIR, RESUME_DIR, VARIANT_DIR, PDF_DIR):
        d.mkdir(parents=True, exist_ok=True)


_rebase(state_store.STATE_DIR / "jobhunt")


def channel_char() -> str:
    """求职通道是谁的信箱（拍板 3：收发物理上走这个号，订阅指着它；发送动作是 server 的，
    不算借用私人信箱）。J2 的草稿落它的草稿信箱、J3 的三分类只挂它的 watcher。"""
    return (os.environ.get("CASSETTE_JOBHUNT_CHANNEL_CHAR") or "").strip() \
        or state_store.DEFAULT_CHAR_ID


# Playwright Chromium（browser 插件装在 state/browser-runtime/）；装了系统 Chrome 也认。
_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def _find_chrome() -> Optional[str]:
    browsers = state_store.STATE_DIR / "browser-runtime" / "browsers"
    for app in sorted(browsers.glob("chromium-*/chrome-mac-*/Google Chrome for Testing.app")):
        binp = app / "Contents" / "MacOS" / "Google Chrome for Testing"
        if binp.exists():
            return str(binp)
    for c in _CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)


def _now_str() -> str:
    return datetime.now(config.APP_TZ).strftime("%Y-%m-%d %H:%M")


# ---------- 需求档案 ----------

def profile_get() -> str:
    if PROFILE_PATH.exists():
        return PROFILE_PATH.read_text("utf-8")
    return ""


def profile_set(text: str) -> dict:
    _atomic_write(PROFILE_PATH, text.strip() + "\n")
    return {"ok": True, "chars": len(text)}


# ---------- 简历仓库 ----------

# rid 直接当文件名用（md / pdf / 下载时的文件名都是它），所以必须挡住路径穿越。
# \w 含中文和下划线、但不含 . 和 /，实测 "../x" "a/b" "a.b" "" "\x00" 全部拒掉——
# 允许中文是为了让简历叫「AI全栈-通用版」而不是「v20260802-191112」。
_RID_RE = re.compile(r"[\w\-]+")


def _variant_path(rid: str) -> Path:
    if not _RID_RE.fullmatch(rid):
        raise ValueError(f"非法简历 id: {rid}")
    return MASTER_PATH if rid == "master" else VARIANT_DIR / f"{rid}.md"


def resume_read(rid: str = "master") -> dict:
    p = _variant_path(rid)
    if not p.exists():
        if rid == "master":
            return {"id": "master", "content": "", "missing": True,
                    "hint": "母版简历还没建。让机主把简历内容发给你，存成 master。"}
        raise KeyError(f"简历不存在: {rid}")
    return {"id": rid, "content": p.read_text("utf-8")}


def resume_save(content: str, rid: Optional[str] = None,
                jd_id: str = "", note: str = "", overwrite: bool = False) -> dict:
    """存简历。rid 不给 → 新定制版（自动起名）；rid="master" → 写母版。
    rid 允许中文，起个人看得懂的名（"字节-iOS"），别用一串时间戳。

    重名直接拒（双角色共挂的防撞规则）：rid 已存在时必须 overwrite=True 才覆盖——
    「我以为在新建，其实盖掉了对方刚改好的版本」这种事故要在结构上灭掉。"""
    if rid is None:
        base = "定制版-" + datetime.now(config.APP_TZ).strftime("%m%d")
        rid, n = base, 1
        while _variant_path(rid).exists():
            n += 1
            rid = f"{base}-{n}"
    p = _variant_path(rid)
    if p.exists() and not overwrite:
        return {"ok": False, "exists": True, "id": rid,
                "hint": f"简历「{rid}」已存在（改于 {datetime.fromtimestamp(p.stat().st_mtime, config.APP_TZ).strftime('%m-%d %H:%M')}）。"
                        "确认要盖掉它就传 overwrite=true；不是同一份就换个 id。"}
    body = content.strip() + "\n"
    if rid != "master":
        front = f"<!-- jd_id: {jd_id} | note: {note} | created: {_now_str()} -->\n"
        if not body.startswith("<!--"):
            body = front + body
    _atomic_write(p, body)
    return {"ok": True, "id": rid}


def resume_list() -> list[dict]:
    out = []
    if MASTER_PATH.exists():
        out.append({"id": "master", "mtime": int(MASTER_PATH.stat().st_mtime)})
    for p in sorted(VARIANT_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        out.append({"id": p.stem, "mtime": int(p.stat().st_mtime)})
    return out


# ---------- markdown → HTML（极简子集，够简历用：标题/加粗斜体/列表/链接/分隔线）----------

# 行尾日期（2024.09-至今 / 2024.09-Present / 2023.10-2024.06），连同前面的分隔符 · 或逗号一起吃掉。
# 「Present」是英文版简历要的：只认「至今」时，英文条目的日期不被抓出来 → 右侧时间轴整条塌掉，
# 日期原样留在标题行里（2026-08-13 做英文简历时踩到）。
# 抓出来单独包 span → 模板里 flex 甩到右边，简历右侧就有一条对齐的时间轴。
_DATE_TAIL = re.compile(
    r"(?:\s*[·,，]\s*)?(\d{4}\.\d{1,2}\s*[-–—]\s*(?:\d{4}\.\d{1,2}|至今|Present))\s*$")


def _inline(s: str) -> str:
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    # 行内代码：先抽出存好、留占位符，免得反引号里的 * 被当成强调。
    # （不支持的时候反引号是原样印在 PDF 上的——简历上出现 ` 是硬伤，踩过）
    codes: list[str] = []

    def _stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    s = re.sub(r"`([^`]+)`", _stash, s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: "<code>" + codes[int(m.group(1))] + "</code>", s)
    return s


def md_to_html(md: str) -> str:
    # 剥掉所有 HTML 注释：除了 resume_save 自动加的 frontmatter，md 里还可能手写改动记录，
    # 都不该出现在投出去的 PDF 上。（原来只剥 \A 开头一个，第二个注释块就被当正文印出来了）
    # 不吃尾随空白：行内注释若连换行一起吃掉，会把上下两行粘成一段。
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    lines = md.splitlines()
    out, in_list, para = [], False, []
    # h1（姓名）+ 紧跟的第一段（联系方式）包进 <header>，模板才排得出抬头区
    seen_h1, header_open, contact_done = False, False, False
    # 「标题行 + 它下面那串 bullet」包进 <section class="item">，模板才能整块避开分页
    # （否则标题落在页底、内容甩到下一页）
    in_item = False

    def flush_para():
        nonlocal para, contact_done
        if not para:
            return
        text = " ".join(para)
        para = []
        if header_open and not contact_done:
            contact_done = True
            out.append('<p class="contact">' + _inline(text) + "</p>")
            close_header()
            return
        m = _DATE_TAIL.search(text)
        if m:
            open_item()
            out.append('<p class="row"><span class="t">' + _inline(text[:m.start()].rstrip())
                       + '</span><span class="d">' + _inline(m.group(1)) + "</span></p>")
        else:
            out.append("<p>" + _inline(text) + "</p>")

    def open_item():
        nonlocal in_item
        close_item()
        out.append('<section class="item">')
        in_item = True

    def close_item():
        nonlocal in_item
        if in_item:
            out.append("</section>")
            in_item = False

    def close_header():
        nonlocal header_open
        if header_open:
            out.append("</header>")
            header_open = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for ln in lines:
        s = ln.rstrip()
        if not s.strip():
            flush_para(); close_list(); continue
        m = re.match(r"(#{1,4})\s+(.*)", s)
        if m:
            flush_para(); close_list(); close_item()
            n = len(m.group(1))
            if n == 1 and not seen_h1:
                seen_h1 = True        # 只有第一个 h1 开 header
                out.append("<header>")
                header_open = True
            else:
                close_header()        # 没有联系方式段就直接收（h1 后紧跟 h2）
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>")
            continue
        if re.match(r"\s*[-*]\s+", s):
            flush_para(); close_header()
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append("<li>" + _inline(re.sub(r"\s*[-*]\s+", "", s, count=1)) + "</li>")
            continue
        if re.match(r"\s*---+\s*$", s):
            flush_para(); close_list(); close_item(); close_header()
            out.append("<hr>"); continue
        para.append(s.strip())
    flush_para(); close_list(); close_item(); close_header()
    return "\n".join(out)


_DEFAULT_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<style>
  @page { size: A4; margin: 15mm 17mm; }
  * { box-sizing: border-box; }
  body { font-family: "PingFang SC", "Helvetica Neue", sans-serif; color: #24292e;
         font-size: 10.5pt; line-height: 1.62; margin: 0; }

  /* 分页保护：标题不落单在页底，条目不被从中间切断 */
  h1, h2, h3 { break-after: avoid; page-break-after: avoid; }
  h2, h3, p, li, header { break-inside: avoid; page-break-inside: avoid; }
  /* 一个条目 = 标题行 + 它下面那串 bullet（md_to_html 包成 section.item）。
     Chromium 不认 p 上的 break-after: avoid，只能靠这个包裹块整体避让，
     否则会出现"标题孤零零留在页底、内容全甩到下一页"。
     比一页还长的条目浏览器会自动放弃 avoid、照常切断，不会溢出。 */
  section.item { break-inside: avoid; page-break-inside: avoid; }

  /* 抬头区：姓名 + 联系方式（md_to_html 包成 <header>） */
  header { margin-bottom: 6mm; padding-bottom: 3.5mm; border-bottom: 2px solid #1f4e79; }
  h1 { font-size: 22pt; letter-spacing: 1px; margin: 0 0 2.5mm;
       color: #1f4e79; font-weight: 600; }
  p.contact { font-size: 9pt; color: #5a6570; margin: 0; }

  h2 { font-size: 11pt; font-weight: 600; letter-spacing: 1.5px; color: #1f4e79;
       border-left: 3px solid #1f4e79; border-bottom: 1px solid #d6e0ea;
       padding: 0 0 1.4mm 2.5mm; margin: 6.5mm 0 3mm; }
  h3 { font-size: 10.5pt; margin: 3mm 0 1mm; }
  p { margin: 1.6mm 0 .8mm; }

  /* 标题行：正文靠左、行尾日期靠右（md_to_html 拆出 .t / .d） */
  p.row { display: flex; justify-content: space-between; align-items: baseline; gap: 6mm; }
  p.row .t strong { color: #16324f; font-weight: 600; }
  p.row .d { white-space: nowrap; font-variant-numeric: tabular-nums;
             font-size: 9pt; color: #7a8b9c; }

  ul { list-style: none; margin: .8mm 0 2.6mm; padding-left: 4.5mm; }
  li { position: relative; margin: 1.1mm 0; }
  li::before { content: "\\25AA"; position: absolute; left: -4mm; top: .2mm;
               color: #4a80b4; font-size: 8pt; }

  strong { font-weight: 600; }
  /* 行内代码：只换等宽字体、不加底色（简历上一块块灰底太花） */
  code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 9.2pt;
         color: #16324f; }
  a { color: #1f4e79; text-decoration: none; }
  hr { border: none; border-top: 1px solid #dde4ea; margin: 4mm 0; }
</style>
</head>
<body>
{{CONTENT}}
</body>
</html>
"""


def _template() -> str:
    if TEMPLATE_PATH.exists():
        return TEMPLATE_PATH.read_text("utf-8")
    _atomic_write(TEMPLATE_PATH, _DEFAULT_TEMPLATE)
    return _DEFAULT_TEMPLATE


def resume_render(rid: str = "master", timeout: int = 60) -> dict:
    """md → 模板 HTML → headless Chrome 出 PDF。返回 {ok, pdf, id}。"""
    info = resume_read(rid)
    if info.get("missing"):
        return {"ok": False, "error": "母版简历是空的，先存内容再渲染"}
    chrome = _find_chrome()
    if not chrome:
        return {"ok": False, "error": "找不到 Chrome/Chromium，PDF 渲染不可用（装 browser 插件会带一只）"}
    html = _template().replace("{{CONTENT}}", md_to_html(info["content"]))
    html_path = PDF_DIR / f".{rid}.{os.getpid()}.{uuid.uuid4().hex[:6]}.html"
    pdf = PDF_DIR / f"{rid}.pdf"
    html_path.write_text(html, "utf-8")
    try:
        proc = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-first-run",
             f"--print-to-pdf={pdf}", "--no-pdf-header-footer",
             f"file://{html_path}"],
            capture_output=True, timeout=timeout)
        if proc.returncode != 0 or not pdf.exists():
            return {"ok": False, "error": f"Chrome 渲染失败: {proc.stderr.decode()[-300:]}"}
        return {"ok": True, "id": rid, "pdf": str(pdf),
                "size_kb": round(pdf.stat().st_size / 1024, 1)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Chrome 渲染超时"}
    finally:
        html_path.unlink(missing_ok=True)


def pdf_path(rid: str) -> Optional[Path]:
    if not _RID_RE.fullmatch(rid):
        return None
    p = PDF_DIR / f"{rid}.pdf"
    return p if p.exists() else None


# ---------- jsonl 底座（小表，读改写全量 + 进程内锁；MCP 全走 HTTP 汇到这个进程）----------

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for ln in path.read_text("utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    _atomic_write(path, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else ""))


def _append_jsonl(path: Path, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------- 岗位库（带状态机）----------

# 状态机（mianmian 时代只是句注释、没人执行，16 条 JD 全卡 scored、sent 从没被写过——
# 搬迁时把它做实）。archived=机主标「不投」，从 sent 之外任何态都能进；unarchive 回
# 打过分就 scored、没打过就 new。
_JD_FLOW: dict[str, set[str]] = {
    "new": {"scored", "drafted", "archived"},   # 不打分直接起草不拦（纪律归 prompt 管）
    "scored": {"scored", "drafted", "archived"},   # scored→scored = override 重打分
    "drafted": {"drafted", "sent", "archived"},    # drafted→drafted = 草稿拟重复，不算事故
    "sent": {"sent"},                              # 发出即终态（重复投递结构上不存在）
    "archived": {"new", "scored"},                 # unarchive
}


def jd_save(source: str, company: str, title: str, text: str, char_id: str = "") -> dict:
    jid = "jd" + datetime.now(config.APP_TZ).strftime("%m%d") + "-" + uuid.uuid4().hex[:4]
    row = {"id": jid, "ts": _now_str(), "source": source, "company": company,
           "title": title, "text": text, "score": None, "score_reason": "",
           "status": "new", "char_id": char_id}
    with _LOCK:
        _append_jsonl(JDS_PATH, row)
    return {"ok": True, "id": jid}


def jd_list(status: Optional[str] = None, limit: int = 30) -> list[dict]:
    rows = _read_jsonl(JDS_PATH)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    rows = rows[-limit:][::-1]
    return [{**{k: r.get(k) for k in ("id", "ts", "source", "company", "title",
                                      "score", "status", "char_id")},
             "text_head": (r.get("text") or "")[:120]} for r in rows]


def companies() -> list[str]:
    """岗位库里出现过的公司名（去重、保留原文、含已归档的）。
    J3 的兜底分类拿它认信：HR 从公司自有域名回过来时，域名表和台账都不认，
    唯一还能对上的线索就是「这家我们库里有」。归档的也要留——岗归档了 HR 照样会回。"""
    seen, out = set(), []
    for r in _read_jsonl(JDS_PATH):
        c = (r.get("company") or "").strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def jd_read(jid: str) -> dict:
    for r in _read_jsonl(JDS_PATH):
        if r.get("id") == jid:
            return r
    raise KeyError(f"岗位不存在: {jid}")


def jd_score(jid: str, score: float, reason: str, char_id: str = "",
             override: bool = False) -> dict:
    """打分（原子）。已打过分的岗默认拒——后到方收到「谁打的、打了多少、理由」，
    要覆盖需明说 override（语义层防重复劳动，拍板 2）。"""
    with _LOCK:
        rows = _read_jsonl(JDS_PATH)
        row = next((r for r in rows if r.get("id") == jid), None)
        if row is None:
            raise KeyError(f"岗位不存在: {jid}")
        cur = row.get("status") or "new"
        if cur == "archived":
            return {"ok": False, "error": f"这条（{row.get('company')}·{row.get('title')}）"
                                          "机主已标「不投」，不用打分了"}
        if cur != "new" and not override:
            by = row.get("char_id") or "（不知谁）"
            return {"ok": False, "already": True, "id": jid, "status": cur,
                    "hint": f"这条 {row.get('company')}·{row.get('title')} 已打过分"
                            f"（{by} 打的 {row.get('score')}，理由：{row.get('score_reason')}），"
                            "状态是 " + cur + "。要覆盖需明说（override=true），"
                            "不然别重复劳动、去看别的岗。"}
        if "scored" not in _JD_FLOW.get(cur, set()) and cur != "new":
            return {"ok": False, "error": f"状态 {cur} 不能回到 scored"}
        row.update(score=float(score), score_reason=reason, status="scored",
                   char_id=char_id or row.get("char_id", ""))
        _write_jsonl(JDS_PATH, rows)
    return {"ok": True, "id": jid, "status": "scored"}


def _jd_set_status(jid: str, to: str, char_id: str = "") -> dict:
    """状态迁移（原子、按 _JD_FLOW 校验）。drafted/sent 由草稿信箱那条路回调。"""
    with _LOCK:
        rows = _read_jsonl(JDS_PATH)
        row = next((r for r in rows if r.get("id") == jid), None)
        if row is None:
            raise KeyError(f"岗位不存在: {jid}")
        cur = row.get("status") or "new"
        if to == cur:
            return {"ok": True, "id": jid, "status": cur}
        if to not in _JD_FLOW.get(cur, set()):
            raise ValueError(f"岗位 {jid} 状态 {cur} → {to} 不是合法迁移")
        row["status"] = to
        if char_id:
            row["char_id"] = char_id
        _write_jsonl(JDS_PATH, rows)
    return {"ok": True, "id": jid, "status": to}


def jd_archive(jid: str) -> dict:
    """机主标「不投」（app 的 JD 库页用；sent 了的不能archive——已经投出去了）。"""
    return _jd_set_status(jid, "archived")


def jd_unarchive(jid: str) -> dict:
    row = jd_read(jid)
    if row.get("status") != "archived":
        return {"ok": True, "id": jid, "status": row.get("status")}
    return _jd_set_status(jid, "scored" if row.get("score") is not None else "new")


# ---------- 投递台账（发出后才有条目；写入方是 mail_bridge 的发送回调）----------

def application_add(app_id: str, jd_id: str, to: str, resume_id: str,
                    message_id: str, char_id: str = "",
                    subject: str = "") -> dict:
    """草稿真发出后落一条台账（id = 草稿 id）。company/title 从 jd 里抄，
    char_id = 经手人（谁起草的，回信硬醒就醒谁）。"""
    company, title = "", ""
    if jd_id:
        try:
            jd = jd_read(jd_id)
            company, title = jd.get("company", ""), jd.get("title", "")
        except KeyError:
            pass
    row = {"id": app_id, "jd_id": jd_id, "company": company, "title": title,
           "to": to, "domain": to.split("@", 1)[1].lower() if "@" in to else "",
           "resume_id": resume_id, "subject": subject,
           "sent_at": _now_str(), "message_id": message_id,
           "status": "sent", "has_reply": False, "char_id": char_id}
    with _LOCK:
        _append_jsonl(APPS_PATH, row)
    if jd_id:
        try:
            _jd_set_status(jd_id, "sent", char_id)
        except (KeyError, ValueError):
            pass   # 台账为准；JD 状态机跟不上（被删/手改）不拦发送记账
    return {"ok": True, "id": app_id}


def applications_list(status: Optional[str] = None, limit: int = 50) -> list[dict]:
    rows = _read_jsonl(APPS_PATH)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows[-limit:][::-1]


def applications_open() -> list[dict]:
    """J3 回信匹配用的匹配集：已发出的全部台账（含已有回信的——HR 会连着来第二封）。"""
    return [r for r in _read_jsonl(APPS_PATH) if r.get("status") == "sent"]


def applications_mark_reply(app_id: str, reply_from: str, reply_subject: str,
                            reply_snippet: str) -> dict:
    with _LOCK:
        rows = _read_jsonl(APPS_PATH)
        for r in rows:
            if r.get("id") == app_id:
                r.update(has_reply=True, reply_at=_now_str(), reply_from=reply_from,
                         reply_subject=reply_subject[:200], reply_snippet=reply_snippet[:400])
                _write_jsonl(APPS_PATH, rows)
                return {"ok": True, "char_id": r.get("char_id", "")}
    return {"ok": False, "error": f"台账没有 {app_id}"}
