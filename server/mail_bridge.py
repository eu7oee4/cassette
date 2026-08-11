"""邮箱桥：TA 自己的信箱（IMAP/SMTP + 授权码直连，默认 163）。

这里是唯一一份收发信代码——mail 插件的 MCP 壳（AI 那条路）和 app.py 的草稿路由
（机主确认那条路）都 import 这里，两条路不会漂移。照 browser_keeper / code_bridge
的成例：插件相关的宿主侧伴生代码住主仓。

口径（机主 2026-08 拍板）：
- **发信白名单**（CASSETTE_MAIL_ALLOW_TO）内直发；白名单外**不发**，落草稿
  state/mail/drafts/，机主在 app 的「草稿信箱」里过目、点发送才真发。
  这是防注入的主锁：来信内容是外部输入，哪怕信里藏了指令，壳层面也发不出去。
- Beacon 笔友的回信**不走这里**——write_letter 的收件参数是卡片编号，走 MCP。
  邮箱只负责收（outlook 旧址自动转发过来）。
- 频控：每小时最多 HOURLY_CAP 封（含草稿确认发出的），发一封记一行 sent_log.jsonl。

163 的坑：登录后必须发一条 IMAP `ID` 命令自报家门，否则报 "Unsafe Login" 拒绝
SELECT（网易反垃圾，见 _imap()）。换别家邮箱只需改 .env 里的 host，ID 命令别家
不认识也无害（容错发送）。

env（见 .env.example）：ADDRESS / AUTH_CODE 必填，其余有默认。授权码是密钥待遇：
只活在 .env，不进对话不入库；改了要重启后端（子进程继承的是启动时那份环境）。
"""
import codecs
import email
import email.policy
import imaplib
import json
import os
import re
import smtplib
import threading
import uuid
from datetime import datetime, timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, getaddresses, parsedate_to_datetime
from pathlib import Path

import config

MAIL_DIR = config.BASE_DIR / "state" / "mail"
DRAFTS_DIR = MAIL_DIR / "drafts"
SENT_LOG = MAIL_DIR / "sent_log.jsonl"

_BODY_CAP = 20000        # 读信正文上限（字符）：防一封巨型 HTML 邮件吃光上下文
_LIST_CAP = 30           # 列表一次最多几封
_LOCK = threading.Lock() # 草稿/日志的进程内互斥；跨进程靠原子替换兜底


class MailError(Exception):
    """带给人看的中文说明的失败。壳/路由捕获后原样转述，不带栈。"""


# ---------- 配置 ----------
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(f"CASSETTE_MAIL_{key}") or default).strip()


def configured() -> bool:
    return bool(_env("ADDRESS") and _env("AUTH_CODE"))


def _cfg() -> dict:
    if not configured():
        raise MailError("邮箱还没配置：把 CASSETTE_MAIL_ADDRESS / CASSETTE_MAIL_AUTH_CODE "
                        "写进 server/.env 再重启后端")
    return {
        "address": _env("ADDRESS"),
        "auth_code": _env("AUTH_CODE"),
        "imap_host": _env("IMAP_HOST", "imap.163.com"),
        "smtp_host": _env("SMTP_HOST", "smtp.163.com"),
        # 分隔符把中英文逗号/分号都认了——这是机主手填的字段，别让一个全角逗号毁掉白名单
        "allow_to": {a.lower() for a in re.split(r"[,，;；\s]+", _env("ALLOW_TO")) if a},
        "hourly_cap": int(_env("HOURLY_CAP", "5") or "5"),
    }


# ---------- IMAP ----------
def _imap(cfg: dict) -> imaplib.IMAP4_SSL:
    try:
        conn = imaplib.IMAP4_SSL(cfg["imap_host"], 993)
        conn.login(cfg["address"], cfg["auth_code"])
    except Exception as e:
        raise MailError(f"连不上邮箱（{cfg['imap_host']}）：{e}") from e
    # 163 反垃圾：登录后不 ID 自报家门，后面的 SELECT 会吃 "Unsafe Login"。
    # 别家不认识 ID 就当没说——失败不拦路。
    try:
        imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
        conn._simple_command("ID", '("name" "cassette" "version" "0.1.0" "vendor" "cassette-mail")')
    except Exception:
        pass
    try:
        conn.select("INBOX")
    except Exception as e:
        raise MailError(f"打不开收件箱：{e}") from e
    return conn


# 转发链路（outlook → 163）给繁体信贴的是 gb2312 标签，但字节其实是 GB18030。
# 按标签严解：信头抛 UnicodeDecodeError（兜底把 =?gb2312?B?..?= 原串吐回去）、
# 正文 errors="replace" 出一片方块。GB18030 是 gb2312/gbk 的超集，一律升格解——
# 只多认字不少认字，对真·gb2312 的信没有副作用。
_CHARSET_UPGRADE = {
    "gb2312": "gb18030", "gb_2312": "gb18030", "gb_2312-80": "gb18030",
    "csgb2312": "gb18030", "euc-cn": "gb18030", "euccn": "gb18030",
    "gbk": "gb18030", "x-gbk": "gb18030", "cp936": "gb18030", "ms936": "gb18030",
}


def _norm_charset(cs) -> str | None:
    """标签 → 真能用的编码名。认不出来的（unknown-8bit 之类）返回 None，让调用方退默认。"""
    cs = (cs or "").strip().strip("\"'").lower()
    if not cs:
        return None
    cs = _CHARSET_UPGRADE.get(cs, cs)
    try:
        codecs.lookup(cs)
    except LookupError:
        return None
    return cs


def _decode_bytes(data: bytes, charset) -> str:
    """按声明的编码解；解不动就依次试 utf-8 / gb18030，全不行才退 replace。
    宁可多试一轮也别出方块——方块是不可逆的，字丢了就找不回来。"""
    cs = _norm_charset(charset) or "utf-8"
    for enc in (cs, "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode(cs, errors="replace")


def _decode_header(raw) -> str:
    if raw is None:
        return ""
    try:
        # 自己把每段字节解成 str 再交给 make_header——它负责的是编码段/非编码段
        # 之间那个空格的还原（"顧墨 <a@b.com>"），解码这步不能交给它。
        chunks = []
        for part, cs in email.header.decode_header(raw):
            if isinstance(part, bytes):
                chunks.append((_decode_bytes(part, cs), _norm_charset(cs)))
            else:
                chunks.append((part, None))
        return str(email.header.make_header(chunks))
    except Exception:
        return str(raw)


def _fmt_date(msg) -> str:
    try:
        dt = parsedate_to_datetime(msg.get("Date")).astimezone(config.APP_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _check_uid(uid: str) -> bytes:
    uid = (uid or "").strip()
    if not uid.isdigit():
        raise MailError(f"uid 不对（要 mail_inbox 给的数字编号）：{uid!r}")
    return uid.encode()


def inbox(limit: int = 10, unread_only: bool = False) -> list[dict]:
    """收件箱摘要，新的在前。只 PEEK 信头，不动已读标记——「扫一眼列表」不算读过。"""
    cfg = _cfg()
    limit = max(1, min(int(limit or 10), _LIST_CAP))
    conn = _imap(cfg)
    try:
        typ, data = conn.uid("search", None, "UNSEEN" if unread_only else "ALL")
        if typ != "OK":
            raise MailError(f"搜信失败：{typ}")
        uids = (data[0] or b"").split()
        out = []
        for uid in reversed(uids[-limit:]):
            typ, parts = conn.uid("fetch", uid,
                                  "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not parts or parts[0] is None:
                continue
            flags = b" ".join(p[0] for p in parts if isinstance(p, tuple))
            header_bytes = b"".join(p[1] for p in parts if isinstance(p, tuple))
            msg = email.message_from_bytes(header_bytes, policy=email.policy.compat32)
            out.append({
                "uid": uid.decode(),
                "from": _decode_header(msg.get("From")),
                "subject": _decode_header(msg.get("Subject")) or "（无主题）",
                "date": _fmt_date(msg),
                "unread": b"\\Seen" not in flags,
            })
        return out
    finally:
        _quiet_logout(conn)


def read_mail(uid: str) -> dict:
    """取一封信的正文（顺手标已读——TA 读过了就是读过了）。text/plain 优先，
    只有 HTML 就剥标签。正文截断到 _BODY_CAP 字符。"""
    cfg = _cfg()
    buid = _check_uid(uid)
    conn = _imap(cfg)
    try:
        typ, parts = conn.uid("fetch", buid, "(BODY.PEEK[])")
        if typ != "OK" or not parts or parts[0] is None:
            raise MailError(f"没找到 uid={uid} 这封信（可能被删了）")
        raw = b"".join(p[1] for p in parts if isinstance(p, tuple))
        msg = email.message_from_bytes(raw, policy=email.policy.compat32)
        body = _extract_body(msg)
        if len(body) > _BODY_CAP:
            body = body[:_BODY_CAP] + f"\n…（太长截断，原文 {len(body)} 字符）"
        conn.uid("store", buid, "+FLAGS", "(\\Seen)")
        return {
            "uid": uid,
            "from": _decode_header(msg.get("From")),
            "to": _decode_header(msg.get("To")),
            "subject": _decode_header(msg.get("Subject")) or "（无主题）",
            "date": _fmt_date(msg),
            "body": body,
        }
    finally:
        _quiet_logout(conn)


def mark(uid: str, action: str) -> str:
    """read / unread 两档。"""
    cfg = _cfg()
    buid = _check_uid(uid)
    if action not in ("read", "unread"):
        raise MailError(f"action 只有 read / unread：{action!r}")
    conn = _imap(cfg)
    try:
        op = "+FLAGS" if action == "read" else "-FLAGS"
        typ, _ = conn.uid("store", buid, op, "(\\Seen)")
        if typ != "OK":
            raise MailError(f"标记失败：{typ}")
        return f"uid={uid} 已标{'已读' if action == 'read' else '未读'}"
    finally:
        _quiet_logout(conn)


def _quiet_logout(conn) -> None:
    try:
        conn.logout()
    except Exception:
        pass


_TAG_RE = re.compile(r"<(?:script|style)[^>]*>.*?</(?:script|style)>", re.S | re.I)
_HTML_RE = re.compile(r"<[^>]+>")


def _extract_body(msg) -> str:
    plain, html = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            text = _decode_bytes(part.get_payload(decode=True), part.get_content_charset())
        except Exception:
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    if plain.strip():
        return plain.strip()
    if html:
        import html as _html
        text = _html.unescape(_HTML_RE.sub("", _TAG_RE.sub("", html)))
        # 剥完标签的 HTML 满是缩进和空行：逐行去空白再压掉连续空行
        lines = [ln.strip() for ln in text.splitlines()]
        return re.sub(r"\n{2,}", "\n\n", "\n".join(lines)).strip()
    return "（没有可读的正文）"


# ---------- 发信 ----------
def _now() -> datetime:
    return datetime.now(config.APP_TZ)


def _sent_last_hour() -> int:
    try:
        lines = SENT_LOG.read_text("utf-8").strip().splitlines()
    except OSError:
        return 0
    cutoff = _now() - timedelta(hours=1)
    n = 0
    for line in reversed(lines[-200:]):
        try:
            ts = datetime.fromisoformat(json.loads(line)["ts"])
        except Exception:
            continue
        if ts < cutoff:
            break
        n += 1
    return n


def _append_sent_log(entry: dict) -> None:
    MAIL_DIR.mkdir(parents=True, exist_ok=True)
    with _LOCK, open(SENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _smtp_send(cfg: dict, to: str, subject: str, body: str) -> None:
    # From 必须就是登录账号（163 硬性要求，否则 DT:SPM 退信）；显示名用 TA 的名字。
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((str(Header(config.agent_name(), "utf-8")), cfg["address"]))
    msg["To"] = to
    msg["Subject"] = Header(subject or "（无主题）", "utf-8")
    try:
        with smtplib.SMTP_SSL(cfg["smtp_host"], 465, timeout=30) as s:
            s.login(cfg["address"], cfg["auth_code"])
            s.sendmail(cfg["address"], [to], msg.as_string())
    except Exception as e:
        raise MailError(f"发送失败（{cfg['smtp_host']}）：{e}") from e


def _check_to(to: str) -> str:
    addrs = [a for _, a in getaddresses([to or ""]) if a]
    if len(addrs) != 1 or "@" not in addrs[0]:
        raise MailError(f"收件人地址不对（一次一封、一个收件人）：{to!r}")
    return addrs[0]


def send(to: str, subject: str, body: str, origin: str = "chat") -> dict:
    """AI 那条路的发信入口。白名单内直发；白名单外落草稿，等机主在 app 里确认。
    返回 {"sent": True, ...} 或 {"drafted": True, "draft_id": ...}。"""
    cfg = _cfg()
    to_addr = _check_to(to)
    if not (body or "").strip():
        raise MailError("正文是空的")
    if to_addr.lower() not in cfg["allow_to"]:
        d = _draft_new(to_addr, subject, body, origin)
        return {"drafted": True, "draft_id": d["id"], "to": to_addr}
    if _sent_last_hour() >= cfg["hourly_cap"]:
        raise MailError(f"这小时发太多了（上限 {cfg['hourly_cap']} 封），缓缓再发")
    _smtp_send(cfg, to_addr, subject, body)
    _append_sent_log({"ts": _now().isoformat(), "to": to_addr,
                      "subject": subject or "", "origin": origin})
    return {"sent": True, "to": to_addr}


# ---------- 草稿信箱（白名单外的信在这排队，机主 app 里过目才发）----------
def _draft_new(to: str, subject: str, body: str, origin: str) -> dict:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    d = {"id": uuid.uuid4().hex[:12], "to": to, "subject": subject or "",
         "body": body, "ts": _now().isoformat(), "origin": origin}
    tmp = DRAFTS_DIR / f".{d['id']}.tmp"
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(DRAFTS_DIR / f"{d['id']}.json")
    return d


_DRAFT_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _draft_path(draft_id: str) -> Path:
    if not _DRAFT_ID_RE.match(draft_id or ""):
        raise MailError(f"草稿编号不合法：{draft_id!r}")
    return DRAFTS_DIR / f"{draft_id}.json"


def drafts_list() -> list[dict]:
    if not DRAFTS_DIR.is_dir():
        return []
    out = []
    for p in DRAFTS_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text("utf-8")))
        except Exception:
            continue
    return sorted(out, key=lambda d: d.get("ts", ""), reverse=True)


def draft_send(draft_id: str) -> dict:
    """机主确认路：这里是白名单的**唯一例外**——人当场看过、人按的键。频控照算。"""
    cfg = _cfg()
    path = _draft_path(draft_id)
    try:
        d = json.loads(path.read_text("utf-8"))
    except OSError:
        raise MailError("这份草稿不在了（可能已经发过或删了）")
    if _sent_last_hour() >= cfg["hourly_cap"]:
        raise MailError(f"这小时发太多了（上限 {cfg['hourly_cap']} 封），缓缓再发")
    _smtp_send(cfg, d["to"], d.get("subject", ""), d.get("body", ""))
    _append_sent_log({"ts": _now().isoformat(), "to": d["to"],
                      "subject": d.get("subject", ""), "origin": "draft_confirm"})
    with _LOCK:
        path.unlink(missing_ok=True)
    return {"sent": True, "to": d["to"]}


def draft_delete(draft_id: str) -> dict:
    path = _draft_path(draft_id)
    with _LOCK:
        found = path.exists()
        path.unlink(missing_ok=True)
    if not found:
        raise MailError("这份草稿不在了")
    return {"ok": True}
