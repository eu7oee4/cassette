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
            "attachments": _extract_attachments(msg, uid),
        }
    finally:
        _quiet_logout(conn)


# 附件的 inline 上限：文本附件带全文（超了截断）；图片带 base64（太大只报名字——
# 进上下文的图有 API 上限，别为一张 10MB 原图撑爆一轮对话）；其余类型只报名字和大小。
_ATT_TEXT_CAP = 20000
_ATT_IMAGE_MAX = 3 * 1024 * 1024


def _safe_filename(name: str) -> str:
    """外部来信的文件名落盘前消毒：去路径分隔和控制字符，限长。空了给个兜底名。"""
    name = re.sub(r"[/\\\x00-\x1f]", "_", (name or "").strip()).strip(". ")
    return name[:80] or "attachment.bin"


def _extract_attachments(msg, uid: str) -> list[dict]:
    """信里的附件 → [{filename, content_type, size, text? | image_b64? | saved_path?}]。
    一期连附件名字都不报，TA 根本不知道有附件（mianmian 那边寄来的信实踩）。
    能进上下文的直接带上（文本附件给全文、不太大的图给 base64）；进不了的（PDF、
    超大图、二进制）落盘 state/mail/attachments/<uid>/，把路径告诉 TA——code 模式里
    TA 自己能打开，机主在 Mac 上也看得到。"""
    out = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        fname = part.get_filename()
        cd = (part.get("Content-Disposition") or "")
        if not fname and not cd.lower().startswith("attachment"):
            continue
        payload = part.get_payload(decode=True) or b""
        ctype = part.get_content_type()
        att = {"filename": _decode_header(fname) if fname else "未命名附件",
               "content_type": ctype, "size": len(payload)}
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            text = _decode_bytes(payload, part.get_content_charset())
            if len(text) > _ATT_TEXT_CAP:
                text = text[:_ATT_TEXT_CAP] + f"\n…（附件太长截断，原文 {len(text)} 字符）"
            att["text"] = text
        elif ctype.startswith("image/") and 0 < len(payload) <= _ATT_IMAGE_MAX:
            import base64
            att["image_b64"] = base64.b64encode(payload).decode("ascii")
        elif payload:
            try:
                d = MAIL_DIR / "attachments" / str(uid)
                d.mkdir(parents=True, exist_ok=True)
                p = d / _safe_filename(att["filename"])
                p.write_bytes(payload)
                att["saved_path"] = str(p)
            except OSError:
                pass   # 落盘失败就只报元数据——附件还在信里，不算丢
        out.append(att)
    return out


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


# ---------- watcher（新邮件 → 醒来的硬触发）----------
# app.py 起一个常驻线程，每 poll_sec() 拍一次 watch_tick()：**网络活动全部关在那个
# 线程里**，wake 的预闸门只读本地 flag 文件，保持纯本地（见 wake.maybe_wake 的口径）。
# 唤醒白名单发件人（WAKE_FROM，默认 = 发信白名单 ∪ beacon@theolorne.com）来信才写
# flag；其他信只推进游标，躺收件箱等自然醒 / 机主让看——机主 2026-08-11 拍板的规则。
WATCH_PATH = MAIL_DIR / "watch.json"                # {"last_uid": N} 已看到哪的游标
WAKE_PENDING_PATH = MAIL_DIR / "wake_pending.json"  # 待醒 flag：[{uid,from,subject}, ...]


def poll_sec() -> int:
    return max(60, int(_env("POLL_SEC", "300") or "300"))


def _wake_from(cfg: dict) -> set[str]:
    raw = _env("WAKE_FROM")
    if raw:
        return {a.lower() for a in re.split(r"[,，;；\s]+", raw) if a}
    return cfg["allow_to"] | {"beacon@theolorne.com"}


def _write_watch(last_uid: int) -> None:
    MAIL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MAIL_DIR / f".watch.{os.getpid()}.tmp"
    tmp.write_text(json.dumps({"last_uid": last_uid}), "utf-8")
    tmp.replace(WATCH_PATH)


def watch_tick() -> None:
    """看一眼有没有新信。游标之后的新 uid：唤醒白名单发件人 → 记进 flag；其余只推进
    游标。**第一拍只立游标不回溯**——别把陈年旧信当成刚到的，一装插件就炸一次醒来。"""
    cfg = _cfg()
    conn = _imap(cfg)
    try:
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK":
            return
        uids = sorted(int(u) for u in (data[0] or b"").split())
        if not uids:
            return
        try:
            last = int(json.loads(WATCH_PATH.read_text("utf-8"))["last_uid"])
        except Exception:
            last = None
        if last is None:
            _write_watch(uids[-1])
            return
        fresh = [u for u in uids if u > last]
        if not fresh:
            return
        wake_from = _wake_from(cfg)
        hits = []
        for u in fresh:
            typ, parts = conn.uid("fetch", str(u).encode(),
                                  "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if typ != "OK" or not parts or parts[0] is None:
                continue
            msg = email.message_from_bytes(
                b"".join(p[1] for p in parts if isinstance(p, tuple)),
                policy=email.policy.compat32)
            addrs = {a.lower() for _, a in getaddresses([msg.get("From") or ""]) if a}
            if addrs & wake_from:
                hits.append({"uid": str(u), "from": _decode_header(msg.get("From")),
                             "subject": _decode_header(msg.get("Subject")) or "（无主题）"})
        # 游标推进和 flag 写入都在成功扫完之后：中途抛异常就整拍作废，下拍重来，
        # 顶多重复看一遍信头，绝不会静默跳过一段 uid。
        _write_watch(uids[-1])
        if hits:
            _merge_wake_pending(hits)
    finally:
        _quiet_logout(conn)


def _merge_wake_pending(hits: list[dict]) -> None:
    with _LOCK:
        try:
            old = json.loads(WAKE_PENDING_PATH.read_text("utf-8"))
        except Exception:
            old = []
        seen = {h["uid"] for h in old}
        merged = old + [h for h in hits if h["uid"] not in seen]
        tmp = MAIL_DIR / f".pending.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(merged, ensure_ascii=False), "utf-8")
        tmp.replace(WAKE_PENDING_PATH)


def consume_wake_pending() -> list[dict]:
    """读并清掉待醒 flag（wake 预闸门用，纯本地、不碰网络）。没有则空列表。"""
    with _LOCK:
        try:
            items = json.loads(WAKE_PENDING_PATH.read_text("utf-8"))
        except Exception:
            return []
        WAKE_PENDING_PATH.unlink(missing_ok=True)
    return items if isinstance(items, list) else []


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
