"""questions：AskUserQuestion 的问答卡（PLAN_chatui §3.5 / U4）。

模型调 AskUserQuestion（不进 allowed_tools）→ CLI 判 ask → can_use_tool 把
那次调用原地挂起，这里登记一张问答卡（**内存 asyncio.Future，不落盘**）+
Bark 推送；卡本体在 assistant 事件流过 translate_events 时就实时推给了 app
（sse "question" 事件，单号=tool_use_id）。机主答完 → REST /questions/decide
→ future 置值 → 回调用 PermissionResultAllow(updated_input=原 input+answers)
回填，工具"执行"出的 tool_result 就是答案原话，模型同轮拿到接着说。

- 回填机制 09-01 探针实证（scratchpad permit_probe.py question 模式）：
  updated_input 带 answers={问题原文: 所选 label} → tool_result =
  'Your questions have been answered: "…"="…"'。自定义答案就是把任意字符串
  当 label 填——CLI 不校验值必须来自 options（探针没验超集字段，别加别的键）。
- **内存态是特性**（同 permits）：后端重启即作废，模型想问会再问。
- **答案只能从后端 REST 来**（app 卡片调）：注入文本怂恿得了他提问，
  替机主作答做不到。
- wake 轮问不了：AskUserQuestion 不在 wake_tools，PreToolUse 门直接拒——
  机主多半不在，问了也是超时。
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import Optional

# 问答只有聊天轮一档（wake 被门挡了）。默认跟 permits 聊天档一样 10 分钟，
# 拍板了改 env。
TIMEOUT_SEC = int(os.environ.get("QUESTION_TIMEOUT_SEC", "600"))

_pending: dict[str, dict] = {}   # 单号 → 卡（含 future/loop；REST 读时剥掉）

_CARD_KEYS = ("id", "char", "questions", "ts", "deadline")


def _live(r: dict) -> bool:
    """还真在等的卡。deadline 界兜回调任务被硬杀、finally 没跑的泄漏——
    豁免判据（waiting）被死卡钉住的话，轮内空闲超时永远不触发
    （[[cassette-wake-gate-bug-class]] 的近亲，permits._live 同款）。"""
    return time.time() <= r["deadline"] + 5 and not r["future"].done()


def pending(char_id: Optional[str] = None) -> list[dict]:
    """待答的卡（app 回前台对齐用）。不带 future 那类进程内脏器。"""
    return sorted(({k: r[k] for k in _CARD_KEYS}
                   for r in list(_pending.values())
                   if _live(r) and (char_id is None or r["char"] == char_id)),
                  key=lambda r: r["ts"])


def waiting(char_id: str) -> bool:
    """这个角色有没有一张挂着等答的卡——轮内空闲超时的豁免判据。"""
    return any(r["char"] == char_id and _live(r)
               for r in list(_pending.values()))


def decide(question_id: str, answers: Optional[dict],
           note: str = "") -> dict:
    """机主作答（REST 调，跑在任意线程）。answers={问题原文: 答案}；
    None＝不想答（模型收到 note 当理由继续说话）。"""
    rec = _pending.get(question_id)
    if rec is None or rec["future"].done():
        return {"ok": False, "error": "没有这张问答卡（可能已超时，"
                                      "或后端重启过——他想问会再问的）"}
    fut: asyncio.Future = rec["future"]

    def _set():
        if not fut.done():
            fut.set_result((answers, (note or "").strip()))

    rec["loop"].call_soon_threadsafe(_set)
    return {"ok": True, "state": "answered" if answers else "dismissed"}


async def ask(char_id: str, *, questions: list[dict],
              question_id: Optional[str] = None,
              timeout: Optional[float] = None
              ) -> Optional[tuple[Optional[dict], str]]:
    """登记一张卡并原地等机主作答。返回 (answers|None, note)；None＝超时。
    卡的清理只有这一处（finally）：答完/不答/超时/轮被掐，都不留悬单。"""
    qid = (question_id or "").strip() or uuid.uuid4().hex[:12]
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    now = int(time.time())
    t = TIMEOUT_SEC if timeout is None else timeout
    _pending[qid] = {"id": qid, "char": char_id,
                     "questions": questions or [],
                     "ts": now, "deadline": now + int(t),
                     "future": fut, "loop": loop}
    _push_card(char_id, questions or [])
    try:
        return await asyncio.wait_for(fut, t)
    except asyncio.TimeoutError:
        return None
    finally:
        _pending.pop(qid, None)


def _push_card(char_id: str, questions: list[dict]) -> None:
    """Bark 只能提醒不能作答。推不出去不影响挂起——app 里照样看得到卡。"""
    try:
        import characters
        from notify import bark_push
        who = characters.display_name(char_id)
        first = ""
        if questions:
            first = str(questions[0].get("question") or "").strip()
        what = first.replace("\n", " ")[:80] or "有个问题想问你"
        bark_push(f"{who}想问：{what}——到 app 里答", title=who)
    except Exception:
        pass
