"""permits：写类调用的原地审批（PLAN_native §1）。

模型的一次写类调用被 CLI 判成 ask → can_use_tool 把那次调用原地挂起，这里
登记一张待批单（**内存 asyncio.Future，不落盘**）+ Bark 推送；机主在 app 权限卡
上拍板 → REST /permits/decide → future 置值：批了，那次调用原地执行、同一轮
继续跑；拒了，模型收到理由、这轮接着说话。超时自动拒。

- **被批的就是挂起的那次调用本身**：单号用 SDK 的 tool_use_id（逐调用唯一），
  参数原样在卡上摆着。批准即执行，不存在「批完再重发」这个环节。
- **内存态是特性**（§1.2）：后端重启即作废（回调随进程一起消失），模型下次
  要做再调一次——裁决没有持久状态可管，也就没有过期/悬单/状态迁移要维护。
- **批准只能从后端 REST 来**（app 卡片调，§1.5）。邮件/论坛/网页里的注入文本
  够得到模型、够不到这个 REST——外面的话最多怂恿他发起调用，弹出来的卡
  还在机主指头底下。
- **卡是署名的**（§2）：两个角色同时想动手就是两张卡，批谁、先批谁、批不批
  都在机主手上——机主本人就是串行化点，没有互斥、没有全局「当前」。
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Optional

# 两档超时（10-29 机主拍板：醒来轮也弹卡——醒来要防的只有安全面，卡有 Bark 推送、
# app 回前台也拉得到）。醒来档长一些：TA 多半不在手机边上。卡挂着时整条 session 被那
# 一轮占着，所以 TA 的消息一到醒来轮的卡自动让路（yield_wake，chat_loop 调）。
# 09-23 到 10-29 之间醒来轮没有卡（PLAN_prs §4 旧口径），那条已改口。
CHAT_TIMEOUT_SEC = int(os.environ.get("PERMIT_TIMEOUT_CHAT_SEC", "600"))
WAKE_TIMEOUT_SEC = int(os.environ.get("PERMIT_TIMEOUT_WAKE_SEC", "1800"))

_pending: dict[str, dict] = {}   # 单号 → 卡（含 future/loop；REST 读时剥掉）

_CARD_KEYS = ("id", "char", "tool", "title", "summary", "said", "note", "ts", "deadline")


def _live(r: dict) -> bool:
    """还真在等的卡。正常路 ask() 的 finally 会清；这条 deadline 界兜的是
    回调任务被硬杀、finally 没跑的泄漏——豁免判据（waiting）要是被一张
    死卡钉住，轮内空闲超时就永远不触发（[[cassette-wake-gate-bug-class]]
    的近亲：看不见的例外面必须有界）。"""
    return time.time() <= r["deadline"] + 5 and not r["future"].done()


def pending(char_id: Optional[str] = None) -> list[dict]:
    """待批的卡（app 回前台对齐用）。不带 future 那类进程内脏器。"""
    return sorted(({k: r[k] for k in _CARD_KEYS}
                   for r in list(_pending.values())
                   if _live(r) and (char_id is None or r["char"] == char_id)),
                  key=lambda r: r["ts"])


def waiting(char_id: str) -> bool:
    """这个角色有没有一次挂着等批的调用——轮内空闲超时的豁免判据：挂起是
    在等机主拍板，流静着是正常的，不是引擎死了。"""
    return any(r["char"] == char_id and _live(r)
               for r in list(_pending.values()))


def decide(permit_id: str, allow: bool, reason: str = "") -> dict:
    """机主拍板（REST 调，跑在任意线程）。单号对不上＝可能已超时自动拒、
    或后端重启作废——有声报，让他想做再来一次。"""
    rec = _pending.get(permit_id)
    if rec is None or rec["future"].done():
        return {"ok": False, "error": "没有这张待批单（可能已超时自动拒了，"
                                      "或后端重启过——他想做会再申请的）"}
    fut: asyncio.Future = rec["future"]

    def _set():
        if not fut.done():
            fut.set_result((bool(allow), (reason or "").strip()))

    rec["loop"].call_soon_threadsafe(_set)
    return {"ok": True, "state": "allowed" if allow else "denied"}


def yield_wake(char_id: str, reason: str) -> int:
    """TA 来了：这个角色醒来轮挂着的卡全部按「拒」收掉（理由原话给模型），回收掉几张。
    聊天轮的卡不动（TA 正看着）。任意线程可调（future 在它自己的 loop 上置值）。"""
    n = 0
    for r in list(_pending.values()):
        if r["char"] != char_id or not r.get("wake") or r["future"].done():
            continue
        fut = r["future"]

        def _set(f=fut):
            if not f.done():
                f.set_result((False, reason))

        r["loop"].call_soon_threadsafe(_set)
        n += 1
    return n


async def ask(char_id: str, *, tool: str, summary: str, title: str = "",
              said: str = "", note: str = "",
              permit_id: Optional[str] = None,
              timeout: Optional[float] = None,
              wake: bool = False) -> Optional[tuple[bool, str]]:
    """登记一张卡并原地等机主拍板。返回 (批没批, 理由)；None＝超时。
    wake=醒来轮弹的（TA 来了会被 yield_wake 收掉）。
    卡的清理只有这一处（finally）：批完/拒完/超时/轮被掐，都不留悬单。"""
    pid = (permit_id or "").strip() or uuid.uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    now = int(time.time())
    t = CHAT_TIMEOUT_SEC if timeout is None else timeout
    _pending[pid] = {"id": pid, "char": char_id, "tool": tool,
                     "title": (title or "").strip()[:300],
                     "summary": (summary or "").strip()[:500],
                     # 他动手前说的人话（PLAN_prs §4.2，卡片最上面）；Bash 的 description
                     "said": (said or "").strip()[:1000],
                     "note": (note or "").strip()[:300],
                     "ts": now, "deadline": now + int(t),
                     "wake": bool(wake),
                     "future": fut, "loop": loop}
    _push_card(char_id, tool, said or summary)
    try:
        return await asyncio.wait_for(fut, t)
    except asyncio.TimeoutError:
        return None
    finally:
        _pending.pop(pid, None)


def _push_card(char_id: str, tool: str, summary: str) -> None:
    """Bark 只能提醒不能拍板（§6）。推不出去不影响挂起——app 里照样看得到卡。"""
    try:
        import characters
        from notify import bark_push_bg
        who = characters.display_name(char_id)
        what = (summary or "").strip().replace("\n", " ")[:80] or tool
        bark_push_bg(f"{who}想动手：{what}——到 app 里批", title=who)   # 事件循环上：不阻塞
    except Exception:
        pass
