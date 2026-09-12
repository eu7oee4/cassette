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

**一人一个信箱**（2026-08-15）：接线走 `characters.mail_conf(char_id)`——.env 的
`CASSETTE_MAIL_*` 是兜底（默认角色零配置即旧行为），char.json 的 mail 段逐键覆盖。
状态（游标、待醒 flag、草稿、发件日志、附件）各自落 `state/characters/<id>/mail/`。
所有公开函数的 `char_id` 都在**参数表末尾且有缺省**，缺省时看 `_cid()`。

env（见 .env.example）：ADDRESS / AUTH_CODE 必填，其余有默认。授权码是密钥待遇：
只活在 .env / char.json，不进对话不入库；改了要重启后端（子进程继承的是启动时那份环境）。
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
from email.message import EmailMessage        # MIMEText 已退役：发信统一走 EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parsedate_to_datetime
from pathlib import Path

import config
from notify import logerr
import state_store

_BODY_CAP = 20000        # 读信正文上限（字符）：防一封巨型 HTML 邮件吃光上下文
_LIST_CAP = 30           # 列表一次最多几封
_LOCK = threading.Lock() # 草稿/日志的进程内互斥；跨进程靠原子替换兜底


class MailError(Exception):
    """带给人看的中文说明的失败。壳/路由捕获后原样转述，不带栈。"""


# ---------- 这一趟是谁的信箱 ----------
def _cid(char_id=None) -> str:
    """显式传参优先；没传就看 `CASSETTE_CHAR_ID`，再退默认角色。

    那个环境变量是 `plugins.mounted()` 按角色注入进 MCP stdio 子进程的（实测：config 的
    env 是**合并**进子进程环境、且**盖得过**继承来的同名值）。靠它，mail 插件一行都不用改
    ——插件照旧调 `mail_bridge.inbox(10, False)`，落到谁的信箱由它自己所在的进程决定。
    ⚠️ 宿主主进程里没有这个变量（所以后端各处调用缺省仍是默认角色）；别在启动后端的
    shell 里 export 它，那会把整个后端的缺省信箱歪掉。"""
    if char_id:
        return char_id
    return (os.environ.get("CASSETTE_CHAR_ID") or "").strip() or state_store.DEFAULT_CHAR_ID


# ---------- 路径（每个角色一套，别再用模块级常量）----------
def _mail_dir(char_id=None) -> Path:
    return state_store.char_state_dir(_cid(char_id)) / "mail"


def _drafts_dir(char_id=None) -> Path:
    return _mail_dir(char_id) / "drafts"


def _sent_log(char_id=None) -> Path:
    return _mail_dir(char_id) / "sent_log.jsonl"


# ---------- 配置 ----------
def _raw(char_id=None) -> dict:
    import characters      # 函数内 import：characters → state_store/config，避免模块级环
    return characters.mail_conf(_cid(char_id))


def configured(char_id=None) -> bool:
    r = _raw(char_id)
    return bool(r["ADDRESS"] and r["AUTH_CODE"])


def _cfg(char_id=None) -> dict:
    r = _raw(char_id)
    if not (r["ADDRESS"] and r["AUTH_CODE"]):
        raise MailError("邮箱还没配置：默认角色写 server/.env 的 CASSETTE_MAIL_ADDRESS / "
                        "CASSETTE_MAIL_AUTH_CODE，其它角色写 characters/<id>/char.json "
                        "的 mail 段（address / auth_code），再重启后端")
    return {
        "address": r["ADDRESS"],
        "auth_code": r["AUTH_CODE"],
        "imap_host": r["IMAP_HOST"] or "imap.163.com",
        "smtp_host": r["SMTP_HOST"] or "smtp.163.com",
        # 分隔符把中英文逗号/分号都认了——这是机主手填的字段，别让一个全角逗号毁掉白名单
        "allow_to": {a.lower() for a in re.split(r"[,，;；\s]+", r["ALLOW_TO"]) if a},
        "hourly_cap": int(r["HOURLY_CAP"] or "5"),
    }


# ---------- IMAP ----------
IMAP_TIMEOUT_SEC = 30   # 单次 socket 操作上限；watcher 每 5 分钟一拍，30s 足够慢网


def _imap(cfg: dict) -> imaplib.IMAP4_SSL:
    try:
        # timeout 必须给（2026-09-12 体检，「闲置致死」类）：不给的话 search/fetch/store 全无
        # 上限，半开连接（Wi-Fi 切换/NAT 老化）让唯一的 watcher 线程永久卡在 recv 上——
        # 异常路径不触发、err_logged 不亮，两个角色的邮件唤醒一起静默失效直到重启。
        conn = imaplib.IMAP4_SSL(cfg["imap_host"], 993, timeout=IMAP_TIMEOUT_SEC)
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
        try:
            conn.logout()   # 09-12：以前抛了就走，连接泄漏
        except Exception:
            pass
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


def inbox(limit: int = 10, unread_only: bool = False, char_id=None) -> list[dict]:
    """收件箱摘要，新的在前。只 PEEK 信头，不动已读标记——「扫一眼列表」不算读过。"""
    cfg = _cfg(char_id)
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


def read_mail(uid: str, char_id=None) -> dict:
    """取一封信的正文（顺手标已读——TA 读过了就是读过了）。text/plain 优先，
    只有 HTML 就剥标签。正文截断到 _BODY_CAP 字符。"""
    cfg = _cfg(char_id)
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
            "attachments": _extract_attachments(msg, uid, char_id),
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


def _extract_attachments(msg, uid: str, char_id=None) -> list[dict]:
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
                d = _mail_dir(char_id) / "attachments" / str(uid)
                d.mkdir(parents=True, exist_ok=True)
                p = d / _safe_filename(att["filename"])
                p.write_bytes(payload)
                att["saved_path"] = str(p)
            except OSError:
                pass   # 落盘失败就只报元数据——附件还在信里，不算丢
        out.append(att)
    return out


def mark(uid: str, action: str, char_id=None) -> str:
    """read / unread 两档。"""
    cfg = _cfg(char_id)
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
def _watch_path(char_id=None) -> Path:       # {"last_uid": N} 已看到哪的游标
    return _mail_dir(char_id) / "watch.json"


def _wake_pending_path(char_id=None) -> Path:  # 待醒 flag：[{uid,from,subject}, ...]
    return _mail_dir(char_id) / "wake_pending.json"


def poll_sec(char_id=None) -> int:
    return max(60, int(_raw(char_id)["POLL_SEC"] or "300"))


def _wake_from(cfg: dict, char_id=None) -> set[str]:
    raw = _raw(char_id)["WAKE_FROM"]
    if raw:
        return {a.lower() for a in re.split(r"[,，;；\s]+", raw) if a}
    return cfg["allow_to"] | {"beacon@theolorne.com"}


def _atomic_write(path: Path, text: str) -> None:
    """临时名带 pid+uuid：多角色是同一个进程里的**多次**调用，只带 pid 会撞
    （CODING_GUIDELINES §3 那条固定 .tmp 名并发写的老坑）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)


def _write_watch(last_uid: int, char_id=None) -> None:
    _atomic_write(_watch_path(char_id), json.dumps({"last_uid": last_uid}))


def watch_tick(char_id=None) -> None:
    """看一眼有没有新信。游标之后的新 uid：唤醒白名单发件人 → 记进 flag；其余只推进
    游标。**第一拍只立游标不回溯**——别把陈年旧信当成刚到的，一装插件就炸一次醒来。
    每个角色各查各的号、各推各的游标（watcher 线程按角色轮着调）。

    求职通道角色（PLAN_jobhunt J3）额外挂**分类**（_jobhunt_watch 判定要不要挂）：
    ① reply     投递回信（精确发件人 / In-Reply-To·References 对台账 Message-ID，不按
                域名——HR 用 163/qq 公共邮箱时按域名会误伤）→ 台账标 has_reply + 待醒 flag；
    ② lead      发件人/主题命中岗位库里已有的公司，或主题命中求职关键词 → 只写待醒 flag；
    ③ subscribe 招聘站订阅 → jd_save(status=new) 入库，不写 flag。
    都不命中就交回唤醒白名单（原来那条路一个字没改）。

    **只有 subscribe 那一路是自动动作**（机主 2026-08-27 拍板）：回信和线索都只是把
    「有这么一封信」送到他眼前，读不读、怎么理解、要不要跟机主说，是他醒来自己的事。
    原来 reply 走的 cohabit 硬醒 + 往 note 里贴正文摘要那段已经退役——外部邮件正文
    不裸进 prompt，口径跟 wake._mail_wake_note 对齐了。"""
    cfg = _cfg(char_id)
    cid = _cid(char_id)
    conn = _imap(cfg)
    try:
        typ, data = conn.uid("search", None, "ALL")
        if typ != "OK":
            # 09-12：以前无声 return——163 限流/"Unsafe Login" 复发时每 5 分钟静默一次，
            # 看起来只是「没新信」。
            logerr(f"mail watcher（{cid}）：IMAP search 返回 {typ}，本拍跳过")
            return
        uids = sorted(int(u) for u in (data[0] or b"").split())
        if not uids:
            return
        try:
            last = int(json.loads(_watch_path(char_id).read_text("utf-8"))["last_uid"])
        except Exception:
            last = None
        if last is None:
            _write_watch(uids[-1], char_id)
            return
        fresh = [u for u in uids if u > last]
        if not fresh:
            return
        wake_from = _wake_from(cfg, char_id)
        jh = _jobhunt_watch(cid)
        hits = []
        for u in fresh:
            # 信头多抓 In-Reply-To/References：三分类的回信匹配要用；别的角色多这两个
            # 字段也无害（几十字节）。正文只在分类命中后按需拉。
            typ, parts = conn.uid("fetch", str(u).encode(),
                                  "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT IN-REPLY-TO REFERENCES)])")
            if typ != "OK" or not parts or parts[0] is None:
                continue
            msg = email.message_from_bytes(
                b"".join(p[1] for p in parts if isinstance(p, tuple)),
                policy=email.policy.compat32)
            addrs = {a.lower() for _, a in getaddresses([msg.get("From") or ""]) if a}
            verdict = _jobhunt_classify(conn, u, msg, addrs, jh, cid) if jh else None
            if verdict is not None:
                flag = _jobhunt_apply(conn, u, verdict, jh, cid)
                if flag:
                    hits.append({"uid": str(u), "from": _decode_header(msg.get("From")),
                                 "subject": _decode_header(msg.get("Subject")) or "（无主题）",
                                 "why": flag["why"]})
                continue
            if addrs & wake_from:
                hits.append({"uid": str(u), "from": _decode_header(msg.get("From")),
                             "subject": _decode_header(msg.get("Subject")) or "（无主题）"})
        # 游标推进和 flag 写入都在成功扫完之后：中途抛异常就整拍作废，下拍重来，
        # 顶多重复看一遍信头，绝不会静默跳过一段 uid。（三分类同规则：分类中途炸了
        # 整拍重来，代价是极小概率同一封订阅邮件入库两条 jd——比静默丢信强。）
        _write_watch(uids[-1], char_id)
        if hits:
            _merge_wake_pending(hits, char_id)
    finally:
        _quiet_logout(conn)


# ---------- jobhunt 三分类（只挂求职通道角色的 watcher，PLAN_jobhunt J3）----------

# 招聘站发件域名表（岗位订阅邮件按它识别；回信匹配**不**按域名）。从 mianmian 原样带。
_RECRUIT_DOMAINS = (
    "zhipin.com", "bosszhipin.com",          # Boss直聘
    "liepin.com", "lietou.com",              # 猎聘
    "linkedin.com",                          # LinkedIn
    "zhaopin.com", "highpin.cn",             # 智联
    "lagou.com",                             # 拉勾
    "51job.com", "mail.51job.com",           # 前程无忧
    "nowcoder.com",                          # 牛客
)
_JD_TEXT_CAP = 3000
_MAIL_FETCH_CAP = 65536   # 分类命中才拉正文，且每封只拉前 64KB（够摘要，防超大附件）

# 主题里出现这些词 → 这封是冲着机主本人来的求职通知，不是群发广告。
# 收得**窄**：「简历」「邀请」「投递」这种单独出现全是招聘站广告文案（实测 08-27 那批
# 猎聘群发：「邀请创造者投递」「简历提升礼包」），进表就是天天误醒。
_JOBHUNT_HINT_WORDS = ("面试", "笔试", "初试", "复试", "终面", "终试", "初筛",
                       "进入下一轮", "投递成功", "录用", "录取", "入职")
# ⚠️ "offer" 试过又拿掉了（机主 08-27 拍板）：实测那 30 封里它只捞到两条招聘站广告
# （「带你锁定心仪Offer！」「实习生offer+实战演练」）。真发 offer 的信会写录用/录取/
# 入职，而且发信方多半已经在岗位库里——公司名那一路每拍重建，本来就接得住。
# 公司名抽出来的英文段落里，这些词太通用，留着必然误伤（'Ant Group' 的 Group）。
_COMPANY_STOP = {"group", "china", "tech", "team", "labs", "data", "info", "global",
                 "limited", "company", "holdings", "intl"}


def _is_recruit(addr: str) -> bool:
    dom = addr.split("@", 1)[1] if "@" in addr else ""
    return any(dom == d or dom.endswith("." + d) for d in _RECRUIT_DOMAINS)


def _company_keys(store) -> set[str]:
    """岗位库里的公司名 → 能在主题/发件人里直接匹配的关键词集合。

    中文名带地名和「有限公司」这类后缀，原样匹配一个都中不了（「杭州拓端数据科技有限
    公司」对不上主题里的「拓端」），所以剥完再补上 2–4 字前缀。
    英文名只收**第一个** ≥4 字母的段：'Ant Group' 的第一段 'Ant' 只有 3 字母就整条放弃，
    绝不让 'Group' 这种词留下来去误伤每一封英文邮件。"""
    keys: set[str] = set()
    try:
        names = store.companies()
    except Exception:
        return keys
    for raw in names:
        # 公司字段里常跟着一长串描述（「ALLTIME万物时（杭州西湖边，10-20人，…）」），
        # 只取第一个分隔符之前的主体。
        core = re.split(r"[·/（(，,、\s|｜【]", raw.strip(), maxsplit=1)[0].strip()
        core = re.sub(r"^(杭州|上海|北京|浙江|深圳|广州|南京|成都|无锡|苏州|中国)", "", core)
        core = re.sub(r"(股份有限公司|有限责任公司|有限公司|集团|股份|公司)$", "", core).strip()
        if core.isascii():
            # 'Ant Group' 按空格切完只剩 'Ant'——3 字母的英文缩写放进关键词表，
            # 等于让每封含 ant 的邮件都来敲门。宁可整条放弃。
            if len(core) >= 4 and core.lower() not in _COMPANY_STOP:
                keys.add(core)
        elif len(core) >= 2:
            keys.add(core)
        if core and "\u4e00" <= core[0] <= "\u9fff":
            for n in (2, 3, 4):
                if len(core) > n and all("\u4e00" <= c <= "\u9fff" for c in core[:n]):
                    keys.add(core[:n])
        m = re.search(r"[A-Za-z]{2,}", core)
        if m and len(m.group()) >= 4 and m.group().lower() not in _COMPANY_STOP:
            keys.add(m.group())
    return keys


def _jobhunt_watch(cid: str) -> dict | None:
    """这个角色的 watcher 这一拍要不要挂分类：得是求职通道角色 + jobhunt 插件启用
    （商店拨开关即时生效，不用重启）。要挂就把台账匹配集和公司名关键词一次建好。
    任何一步取不到都返回 None——分类挂不上不该拖垮基础 watcher。"""
    try:
        import jobhunt_store
        if cid != jobhunt_store.channel_char():
            return None
        import plugins
        if not plugins._read_enabled(cid).get("jobhunt"):
            return None
        apps = jobhunt_store.applications_open()
        return {"store": jobhunt_store,
                "by_addr": {a["to"].lower(): a for a in apps if a.get("to")},
                "by_mid": {a["message_id"]: a for a in apps if a.get("message_id")},
                "companies": _company_keys(jobhunt_store)}
    except Exception:
        return None


def _fetch_body_text(conn, uid: int) -> str:
    """按需拉一封信的正文纯文本（前 64KB）。解析失败返回空串——宁缺勿错。"""
    try:
        typ, parts = conn.uid("fetch", str(uid).encode(),
                              f"(BODY.PEEK[]<0.{_MAIL_FETCH_CAP}>)")
        if typ != "OK" or not parts or parts[0] is None:
            return ""
        raw = b"".join(p[1] for p in parts if isinstance(p, tuple))
        return _extract_body(email.message_from_bytes(raw, policy=email.policy.compat32))
    except Exception:
        return ""


def _jobhunt_classify(conn, uid: int, msg, addrs: set[str], jh: dict, cid: str) -> dict | None:
    """一封新信过分类。返回 None = jobhunt 不认这封（交回唤醒白名单）；
    否则返回 {"kind", "why", ...}，**动作由 watch_tick 执行**，这里只判不做。

    ① reply     台账精确发件人，或 Message-ID 子串命中 In-Reply-To/References
    ② lead      发件人显示名/域名/主题命中岗位库里已有的公司，或主题命中求职关键词
    ③ subscribe 发件域名命中招聘站表

    顺序是 reply → lead → subscribe，**冲着机主本人来的排在群发订阅前面**：
    牛客代发的笔试邀请要是先撞上域名表，就会被当成订阅静默入库，反而更糟。

    机主 2026-08-27 拍板的口径：reply / lead **只写待醒 flag，不硬醒、不往 prompt 里
    贴正文**——「你自己看到回信，对照投递情况、信件内容，再告诉我」。自动动作只剩
    subscribe 那一路入库。"""
    subject = _decode_header(msg.get("Subject")) or "（无主题）"
    from_name = _decode_header(msg.get("From")) or ""
    from_addr = next(iter(addrs), "")
    app = next((jh["by_addr"][a] for a in addrs if a in jh["by_addr"]), None)
    if app is None:
        refs = " ".join([(msg.get("In-Reply-To") or ""), (msg.get("References") or "")])
        app = next((rec for mid, rec in jh["by_mid"].items() if mid and mid in refs), None)
    if app is not None:
        return {"kind": "reply", "app": app, "subject": subject, "from": from_addr,
                "why": "投递台账里匹配上了这封的收件人/Message-ID"}
    hay = f"{from_name} {from_addr} {subject}"
    hit = next((c for c in (jh.get("companies") or set()) if c in hay), "")
    if hit:
        return {"kind": "lead", "why": f"岗位库里有「{hit}」这家"}
    word = next((w for w in _JOBHUNT_HINT_WORDS if w in subject.lower()), "")
    if word:
        return {"kind": "lead", "why": f"主题里有「{word}」"}
    if any(_is_recruit(a) for a in addrs):
        return {"kind": "subscribe", "subject": subject, "from": from_addr}
    return None


def _jobhunt_apply(conn, uid: int, verdict: dict, jh: dict, cid: str) -> dict | None:
    """执行分类结论。返回一条待醒 flag（None = 这封不用惊动谁）。

    reply 会顺手把台账标成 has_reply —— 那是**记账**，不是替他判断：谁回了、什么时候
    回的、摘要是什么，本来就该落在台账里给 app 和 applications_list 看。醒来 prompt 里
    照旧只给信头。"""
    if verdict["kind"] == "subscribe":
        body = _fetch_body_text(conn, uid)
        jh["store"].jd_save(source=f"邮件订阅({verdict['from']})", company="",
                            title=verdict["subject"][:120],
                            text=re.sub(r"\s+", " ", body).strip()[:_JD_TEXT_CAP])
        return None
    if verdict["kind"] == "reply":
        app = verdict["app"]
        snippet = re.sub(r"\s+", " ", _fetch_body_text(conn, uid)).strip()[:400]
        try:
            jh["store"].applications_mark_reply(app.get("id", ""), verdict["from"],
                                                verdict["subject"], snippet)
        except Exception:
            from notify import logerr
            logerr(f"jobhunt：台账标 has_reply 失败（{app.get('id', '')}），flag 照写")
        who = app.get("company") or app.get("to", "")
        return {"why": f"{verdict['why']}（{who}）"}
    return {"why": verdict["why"]}


def _merge_wake_pending(hits: list[dict], char_id=None) -> None:
    path = _wake_pending_path(char_id)
    with _LOCK:
        try:
            old = json.loads(path.read_text("utf-8"))
        except Exception:
            old = []
        seen = {h["uid"] for h in old}
        merged = old + [h for h in hits if h["uid"] not in seen]
        _atomic_write(path, json.dumps(merged, ensure_ascii=False))


def consume_wake_pending(char_id=None) -> list[dict]:
    """读并清掉这个角色的待醒 flag（wake 预闸门用，纯本地、不碰网络）。没有则空列表。
    flag 是消费式的，且**各人一份**——不会再出现「一个角色把写给另一个角色的信
    的唤醒吞掉」。"""
    path = _wake_pending_path(char_id)
    with _LOCK:
        try:
            items = json.loads(path.read_text("utf-8"))
        except Exception:
            return []
        path.unlink(missing_ok=True)
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


def _sent_last_hour(char_id=None) -> int:
    try:
        lines = _sent_log(char_id).read_text("utf-8").strip().splitlines()
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


def _append_sent_log(entry: dict, char_id=None) -> None:
    path = _sent_log(char_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _smtp_send(cfg: dict, to: str, subject: str, body: str, char_id=None,
               attachment: Path | None = None, attach_name: str = "") -> str:
    """发出并返回 Message-ID（jobhunt 台账存它，回信按 In-Reply-To/References 精确匹配）。
    attachment 只能是 server 侧自己解析出的路径（简历 PDF）——工具面不收任意路径。

    2026-08-21 从 MIMEText 换成 EmailMessage：附件要 add_attachment，且要拿得到
    Message-ID。unicode 信头交给默认 policy 编码，行为不变。"""
    msg = EmailMessage()
    # From 必须就是登录账号（163 硬性要求，否则 DT:SPM 退信）；显示名用发信这位的名字。
    # 从前这里是 display_name(owner_of("mailbox"))——那时候信箱是独占资源、全机只有一个。
    # 现在一人一个号，署名当然是**这封信是谁发的**，不然 Cass 发的信落款会是 TA 的名字。
    import characters
    msg["From"] = formataddr((str(Header(characters.display_name(_cid(char_id)),
                                         "utf-8")), cfg["address"]))
    msg["To"] = to
    msg["Subject"] = subject or "（无主题）"
    mid = make_msgid(domain=cfg["address"].split("@", 1)[1] if "@" in cfg["address"] else None)
    msg["Message-ID"] = mid
    msg.set_content(body)
    if attachment is not None:
        # 附件名 HR 那头看得懂（email_draft 传的 attach_name），不带 .pdf 自动补
        name = attach_name.strip() or attachment.name
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        msg.add_attachment(attachment.read_bytes(), maintype="application",
                           subtype="pdf", filename=name)
    try:
        with smtplib.SMTP_SSL(cfg["smtp_host"], 465, timeout=30) as s:
            s.login(cfg["address"], cfg["auth_code"])
            s.send_message(msg)
    except Exception as e:
        raise MailError(f"发送失败（{cfg['smtp_host']}）：{e}") from e
    return mid


def _check_to(to: str) -> str:
    addrs = [a for _, a in getaddresses([to or ""]) if a]
    if len(addrs) != 1 or "@" not in addrs[0]:
        raise MailError(f"收件人地址不对（一次一封、一个收件人）：{to!r}")
    return addrs[0]


def send(to: str, subject: str, body: str, origin: str = "chat", char_id=None) -> dict:
    """AI 那条路的发信入口。白名单内直发；白名单外落草稿，等机主在 app 里确认。
    返回 {"sent": True, ...} 或 {"drafted": True, "draft_id": ...}。
    白名单、频控、草稿都是**这个角色自己那份**。"""
    cfg = _cfg(char_id)
    to_addr = _check_to(to)
    if not (body or "").strip():
        raise MailError("正文是空的")
    if to_addr.lower() not in cfg["allow_to"]:
        d = _draft_new(to_addr, subject, body, origin, char_id)
        return {"drafted": True, "draft_id": d["id"], "to": to_addr}
    if _sent_last_hour(char_id) >= cfg["hourly_cap"]:
        raise MailError(f"这小时发太多了（上限 {cfg['hourly_cap']} 封），缓缓再发")
    _smtp_send(cfg, to_addr, subject, body, char_id)
    _append_sent_log({"ts": _now().isoformat(), "to": to_addr,
                      "subject": subject or "", "origin": origin}, char_id)
    return {"sent": True, "to": to_addr}


# ---------- 草稿信箱（白名单外的信在这排队，机主 app 里过目才发）----------
def _draft_new(to: str, subject: str, body: str, origin: str, char_id=None,
               extra: dict | None = None) -> dict:
    d = {"id": uuid.uuid4().hex[:12], "to": to, "subject": subject or "",
         "body": body, "ts": _now().isoformat(), "origin": origin}
    if extra:
        d.update(extra)   # jobhunt 草稿的附加字段（resume_id/jd_id/attach_name/char_id…）
    _atomic_write(_drafts_dir(char_id) / f"{d['id']}.json",
                  json.dumps(d, ensure_ascii=False, indent=2))
    return d


def jobhunt_draft(to: str, subject: str, body: str, resume_id: str,
                  jd_id: str = "", attach_name: str = "", by_char: str = "") -> dict:
    """jobhunt 的 email_draft 入口（PLAN_jobhunt 拍板 1/3）：草稿落**求职通道角色**的
    草稿信箱（不管起草的是谁——发送物理上走那个号），附件记 resume_id 不记路径，
    draft_send 时 server 侧现场解析 pdf/<id>.pdf。落箱即 Bark 提醒机主。
    by_char = 经手人（HR 回信硬醒就醒 TA）。"""
    import jobhunt_store
    channel = jobhunt_store.channel_char()
    to_addr = _check_to(to)
    if not (subject or "").strip() or not (body or "").strip():
        raise MailError("主题/正文不能为空")
    # 附件必须来自简历库且已渲染（不收任意路径；这也是"附件来源白名单"的全部实现）
    if not jobhunt_store.pdf_path(resume_id):
        raise MailError(f"简历「{resume_id}」还没渲染成 PDF——先 resume_render 再起草")
    company, title = "", ""
    if jd_id:
        try:
            jd = jobhunt_store.jd_read(jd_id)
            company, title = jd.get("company", ""), jd.get("title", "")
        except KeyError:
            raise MailError(f"岗位不存在：{jd_id}（jd_list 里现查一下 id）")
    d = _draft_new(to_addr, subject, body, "jobhunt", channel, extra={
        "resume_id": resume_id, "jd_id": jd_id,
        "attach_name": (attach_name or "").strip() or f"简历-{resume_id}.pdf",
        "char_id": by_char, "company": company, "title": title})
    if jd_id:
        try:
            jobhunt_store._jd_set_status(jd_id, "drafted", by_char)
        except (KeyError, ValueError):
            pass   # 草稿是主体，JD 状态跟不上不拦起草
    # Bark 丢线程发（同步调会拖长 MCP 那头的等待；code-stop 那次踩过 hook 超时的坑）
    import characters
    from notify import bark_push
    who = characters.display_name(by_char) if by_char else "TA"
    target = f"{company}·{title}" if (company or title) else to_addr
    threading.Thread(target=bark_push,
                     args=(f"{who}起草了投递 {target}，去草稿信箱确认",
                           characters.display_name(channel)),
                     daemon=True).start()
    return {"drafted": True, "draft_id": d["id"], "to": to_addr,
            "attach_name": d["attach_name"],
            "note": f"已落草稿信箱（{characters.display_name(channel)}的号）等机主确认，"
                    "已 Bark 提醒。你没有发送能力，别答应「我这就发出去」。"}


_DRAFT_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def _draft_path(draft_id: str, char_id=None) -> Path:
    if not _DRAFT_ID_RE.match(draft_id or ""):
        raise MailError(f"草稿编号不合法：{draft_id!r}")
    return _drafts_dir(char_id) / f"{draft_id}.json"


def drafts_list(char_id=None) -> list[dict]:
    d = _drafts_dir(char_id)
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.json"):
        try:
            out.append(json.loads(p.read_text("utf-8")))
        except Exception:
            continue
    return sorted(out, key=lambda d: d.get("ts", ""), reverse=True)


def draft_send(draft_id: str, char_id=None) -> dict:
    """机主确认路：这里是白名单的**唯一例外**——人当场看过、人按的键。频控照算。"""
    cfg = _cfg(char_id)
    path = _draft_path(draft_id, char_id)
    try:
        d = json.loads(path.read_text("utf-8"))
    except OSError:
        raise MailError("这份草稿不在了（可能已经发过或删了）")
    if _sent_last_hour(char_id) >= cfg["hourly_cap"]:
        raise MailError(f"这小时发太多了（上限 {cfg['hourly_cap']} 封），缓缓再发")
    # jobhunt 草稿带附件：resume_id 现场解析成 PDF 路径（草稿里从不存路径）
    attachment = None
    if d.get("resume_id"):
        import jobhunt_store
        attachment = jobhunt_store.pdf_path(d["resume_id"])
        if attachment is None:
            raise MailError(f"简历 PDF 不在了（{d['resume_id']}）——让 TA 重新渲染再寄")
    mid = _smtp_send(cfg, d["to"], d.get("subject", ""), d.get("body", ""), char_id,
                     attachment=attachment, attach_name=d.get("attach_name", ""))
    _append_sent_log({"ts": _now().isoformat(), "to": d["to"],
                      "subject": d.get("subject", ""), "origin": "draft_confirm"}, char_id)
    if d.get("origin") == "jobhunt":
        # 投递台账在**真发出**这一刻落（sent 态第一次有人写）；经手人从草稿里带
        import jobhunt_store
        jobhunt_store.application_add(draft_id, d.get("jd_id", ""), d["to"],
                                      d.get("resume_id", ""), mid,
                                      char_id=d.get("char_id", ""),
                                      subject=d.get("subject", ""))
    with _LOCK:
        path.unlink(missing_ok=True)
    return {"sent": True, "to": d["to"]}


def draft_delete(draft_id: str, char_id=None) -> dict:
    path = _draft_path(draft_id, char_id)
    with _LOCK:
        found = path.exists()
        path.unlink(missing_ok=True)
    if not found:
        raise MailError("这份草稿不在了")
    return {"ok": True}
