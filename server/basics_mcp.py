"""基础工具 MCP（stdio）：now（真实时间）+ fetch（取一个公开网页）+ next_wake（闹钟）。

主仓内置、不走插件商店（同 skills_mcp / pet_mcp 口径）：这几件不是"能力扩展"，
是他每一轮都该够得着的常识器官。三个场景无差别照挂、无开关。

**now 为什么必须存在**（2026-09-02）：模型没有钟。它对"现在几点"的全部知识
就是上下文里的字。我们在每轮注入头写一句【MM-dd 周X HH:mm】，那是服务端组请求
那一刻读的真时间——但那是**轮首**的读数。一轮里翻十个网页、读半天邮件之后，
他手上没有任何办法知道现在几点，只能拿轮首那句往下推，推错的单位是小时甚至天。
（那天他把 3 分钟前说的话说成「昨天」，根因之一就是手上没有钟。）
now 返回的是系统读数，不是他自己写的字——这条区别是整套核实纪律的地基。

**next_wake 为什么从标记搬成工具**（2026-09-03，PLAN_native §14.1）：
`[[next_wake:…]]` 是写在自然语言正文里的控制标记，解析器只做模式匹配，分不出
「他在下指令」和「他在引用」——09-02 22:46 实锤，他把自己的注入原样贴了一遍，
里面的例子当场把钟改了，正文还被剥出个窟窿。**工具调用天生分得出**：贴一段
包含工具调用的文本不会触发那个工具。顺带补上标记做不到的三件——取消（clear）、
查（read）、以及**当场回执**（旧路是轮尾静默生效，他不知道自己刚定了什么、
被顶掉的旧钟也凭空消失）。

**fetch 为什么值得和 browser 并存**：browser 功能全（能点能填能截图），但它
要起 Chrome、会崩、快照进上下文是大块 token。取一份官方公开信息（文档、公告、
API 页）根本不需要一个浏览器——fetch 一次 HTTP，返回纯文本，稳且省。
browser 留给"要交互 / 要登录态 / 要看渲染后的样子"。

安全面：
- 只许 http/https，只许公网地址。**私网/回环一律拒**——后端自己在 :8000、
  mianmian 在 :8765、MCP token 在环境里，一个能取任意 URL 的工具指回内网就是
  SSRF。域名先解析再判，防 DNS 指回 127.0.0.1 那一手。
- 不跟随跨到私网的跳转（每一跳重判）。
- 大小/超时都有上限；截断**必须在返回体里说话**（jd_list 那次的教训：光秃秃
  给一页、调用方当成全部，据此宣布"不存在"）。
"""
import ipaddress
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import pipeline
import state_store

CHAR_ID = os.environ.get("CASSETTE_CHAR_ID", "default")

mcp = FastMCP("basics")

FETCH_TIMEOUT = 20
FETCH_MAX_BYTES = 2 * 1024 * 1024      # 下载上限（超了直接不读）
FETCH_MAX_CHARS = 20_000               # 交到他手上的正文上限
MAX_REDIRECTS = 5
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"


# ---------- SSRF 闸 ----------

# 明确列出要挡的网段，而不是用 ip.is_private 一刀切。
# 一刀切在这台机器上会把**所有**站点挡掉：本机 DNS 走代理，公网域名被映射进
# 198.18.0.0/15（IANA benchmarking 段，Python 的 is_private 认它），example.com
# 解析出来就是 198.18.0.57。那个段在这儿是代理的转发入口，不是能连到的内网服务，
# 放行；真要收紧得先搞清楚这台机器的代理拓扑，别拿"看着像内网"当判据。
# 100.64.0.0/10 必须挡：Tailscale 的 tailnet 在那儿（手机的 tailnet 地址就在这一段）。
_BLOCKED = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",   # ← tailnet
    "224.0.0.0/4", "240.0.0.0/4",
    "::1/128", "fc00::/7", "fe80::/10", "ff00::/8",
)]


def _public_ip(host: str) -> None:
    """域名解析到的**每一个**地址都得在射程内，否则整体拒——不挑着连，
    挑就等于给了绕过的口子（多记录里混一条内网地址是经典的 SSRF 手法）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ValueError(f"域名解析不了：{host}（{e}）")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if any(ip in net for net in _BLOCKED):
            raise ValueError(
                f"{host} 解析到内网地址 {ip}——fetch 只取公网上的东西。"
                "本机/内网/tailnet 的服务不在它的射程里（那些用别的工具看）。")


def _check_url(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https"):
        raise ValueError(f"只支持 http/https，给的是 {u.scheme or '（空）'}：{url}")
    if not u.hostname:
        raise ValueError(f"URL 里没有域名：{url}")
    _public_ip(u.hostname)
    return url


# ---------- HTML → 正文 ----------

_DROP = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>",
                   re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_BLANK = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    import html as _h
    s = _DROP.sub(" ", html)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|tr|h[1-6])>", "\n", s, flags=re.I)
    s = _TAG.sub("", s)
    s = _h.unescape(s)
    s = "\n".join(line.strip() for line in s.splitlines())
    return _BLANK.sub("\n\n", s).strip()


# ---------- 工具 ----------

@mcp.tool()
def now() -> dict:
    """现在几点——**服务端系统时钟的真实读数**，不是从上下文里推的。

    一轮里做了一串事之后想知道当下时间，问这个。轮首注入里那句时间是这一轮
    **开始**时的读数，中间隔了多少工具调用它是不知道的。
    要说"现在几点""过了多久""是不是该睡了"这类话之前，先问一次。"""
    ts = int(time.time())
    return {"ts": ts, "time": pipeline.now_str(), "stamp": pipeline.stamp_str(ts)}


def _slot(sched: dict) -> tuple:
    """schedule 里那一个槽 → (时点或 None, 待办)。读坏了当没有钟。"""
    try:
        at = sched.get("next_wake_at")
        at = int(float(at)) if at is not None else None
    except (TypeError, ValueError):
        at = None
    return at, (sched.get("next_wake_todo") or "").strip()


@mcp.tool()
def next_wake(action: Literal["read", "set", "clear"] = "read",
              minutes: int = 0, todo: str = "") -> dict:
    """你的闹钟：到点会有一次醒来，`todo` 那句话原样递回给你。

    action：
      · "read"  —— 看现在钉着什么（不改任何东西）
      · "set"   —— 定/改：`minutes`＝多久之后（5~720 分钟），`todo`＝到时候要做的事
      · "clear" —— 撤掉，之后不再自己醒

    ⚠️ **钟只有一个。** set 一次是把原来那张**换掉**，不是再加一张——回执会告诉你
    换掉的是什么。想留着原来那个点，这轮就别 set；不确定现在钉着什么，先 read。

    `todo` 写清楚要做的事（「给谁回信」「读第 2 封信」），别写「继续」——到点递回给
    你的就是这一句，读不懂它的那次醒来是白醒。"""
    act = (action or "").strip().lower()
    if act not in ("read", "set", "clear"):
        raise ValueError(f"action 只能是 read / set / clear，给的是 {action!r}")

    # ⚠️ SCHEDULE_LOCK 是线程锁，**跨不了进程**——这里是 MCP 子进程，服务端那边
    # finalize/finish_wake_turn 也在写同一份 schedule.json。加它只保本进程内一致；
    # 跨进程靠 _write_json 的原子替换保证不出半截文件，丢更新的窗口是毫秒级。
    # （真要根治得换文件锁，那是单槽位这个形状本身的账，记在 §14.6。）
    with state_store.SCHEDULE_LOCK:
        sched = state_store.read_schedule(CHAR_ID)
        old_at, old_todo = _slot(sched)
        old_desc = pipeline.alarm_slot_str(old_at, old_todo) if old_at else ""

        if act == "read":
            if not old_at:
                return {"ok": True, "set": False, "text": "现在没有钟。"}
            left = old_at - int(time.time())
            if left > 0:
                return {"ok": True, "set": True, "at": old_at, "todo": old_todo,
                        "text": f"现在钉着 {old_desc}（{pipeline.fmt_gap(left)}后）。"}
            # 到点了还留着＝那次醒来没走完（机制会自己清，不看他做没做）。
            # 措辞不许暗示他失约——§14.3 那条口径。
            return {"ok": True, "set": True, "at": old_at, "todo": old_todo, "due": True,
                    "text": f"{old_desc} 已经到点了（过了 {pipeline.fmt_gap(-left)}），"
                            "那次醒来还没走完。机制会自己清掉它，你不用管。"}

        if act == "set":
            mins = pipeline.clamp_next_minutes(minutes)
            if mins is None:
                raise ValueError(
                    f"minutes 要是个正整数（{pipeline.NEXT_MIN_MIN}~"
                    f"{pipeline.NEXT_MAX_MIN} 分钟），给的是 {minutes!r}。钟没有动。")
            at = int(time.time()) + mins * 60
            new_todo = (todo or "").strip()[:pipeline.NEXT_TODO_MAX]
            sched["next_wake_at"] = at
            # 待办跟着时点整体替换（没写就是清空）：钉子换了地方，旧的那句活就作废了。
            sched["next_wake_todo"] = new_todo
            state_store.write_schedule(sched, CHAR_ID)
            new_desc = pipeline.alarm_slot_str(at, new_todo)
            tail = (f"原来那张（{old_desc}）已经不在了——钟只有一个。"
                    if old_desc else "原来没有钟。")
            pipeline.logerr(f"[{CHAR_ID}] next_wake set：{new_desc}"
                            + (f"（顶掉 {old_desc}）" if old_desc else "（原来没有钟）"))
            return {"ok": True, "set": True, "at": at, "todo": new_todo,
                    "replaced": old_desc,
                    "text": f"定在 {new_desc}（{pipeline.fmt_gap(mins * 60)}后）。{tail}"}

        # clear
        sched["next_wake_at"] = None
        sched["next_wake_todo"] = ""
        state_store.write_schedule(sched, CHAR_ID)
        pipeline.logerr(f"[{CHAR_ID}] next_wake clear："
                        + (f"撤掉 {old_desc}" if old_desc else "本来就没有钟"))
        return {"ok": True, "set": False, "replaced": old_desc,
                "text": (f"撤掉了 {old_desc}，现在没有钟。" if old_desc
                         else "本来就没有钟，没什么可撤的。")}


@mcp.tool()
def fetch(url: str, max_chars: int = FETCH_MAX_CHARS) -> dict:
    """取一个**公开**网页/接口，返回纯文本（HTML 会剥成正文）。

    什么时候用它而不是 browser：查官方文档、公告、API 返回、一篇文章——只要
    "打开就能看到、不用登录也不用点"，fetch 更稳更省（不起 Chrome，不会崩，
    返回的是文本不是整页快照）。
    什么时候必须用 browser：要登录态、要点/填/滚动、要看渲染后的样子、要截图。

    ⚠️ 只取公网。指向 localhost / 内网的地址会被拒。
    ⚠️ 正文超长会截断，返回体里 truncated=true 并告诉你截了多少——**截断处
    看不到的内容不等于不存在**，需要全文就换更具体的 URL 或分段取。"""
    seen = []
    target = _check_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(target, headers={
            "User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"})
        try:
            opener = urllib.request.build_opener(_NoRedirect)
            resp = opener.open(req, timeout=FETCH_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                nxt = urllib.parse.urljoin(target, e.headers["Location"])
                seen.append(target)
                target = _check_url(nxt)          # 每一跳重判，不许跳进内网
                continue
            raise ValueError(f"HTTP {e.code}：{e.reason}（{target}）")
        except urllib.error.URLError as e:
            raise ValueError(f"取不到 {target}：{e.reason}")
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(FETCH_MAX_BYTES + 1)
            if len(raw) > FETCH_MAX_BYTES:
                raise ValueError(f"这个地址回的东西超过 {FETCH_MAX_BYTES // 1024}KB，"
                                 "没读——换个更具体的 URL")
            enc = "utf-8"
            m = re.search(r"charset=([\w-]+)", ctype)
            if m:
                enc = m.group(1)
            text = raw.decode(enc, errors="replace")
            if "html" in ctype or text.lstrip()[:200].lower().startswith("<!doctype html"):
                text = _html_to_text(text)
            cap = max(500, int(max_chars))
            out = text[:cap]
            return {
                "url": resp.geturl(), "status": resp.status,
                "content_type": ctype.split(";")[0].strip(),
                "redirects": seen,
                "chars": len(text), "shown": len(out),
                "truncated": len(out) < len(text),
                "hint": (f"只给了前 {len(out)} 字，全文 {len(text)} 字——"
                         "**没显示出来的不等于不存在**，要后面的内容换更具体的 URL"
                         if len(out) < len(text) else ""),
                "text": out,
            }
    raise ValueError(f"跳转超过 {MAX_REDIRECTS} 次，停了：{url}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """自动跟随会绕过 SSRF 闸（第一跳公网、第二跳 127.0.0.1）。
    关掉自动跟随，每一跳回到 fetch 里重新过 _check_url。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


if __name__ == "__main__":
    mcp.run()
