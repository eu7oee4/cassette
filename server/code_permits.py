"""code_permits：写权限的带外批准门（PLAN_sdk §5.3 / S3 PR14-c）。

code 不是模式，是一次写权限申请（08-31 改判）：挂载=session 级，放行=轮级。
这里管「放行」那半的状态：申请 → app 弹窗（TA 在场）/ Bark 待批（不在场）→
批准落**这份后端状态** → PreToolUse 门查记录不查对话——邮件/论坛/网页里的
注入文本够不到这扇门（外面的话只能怂恿他递申请，批不批永远在 TA 手上）。

纪律：
- **一场一批、收摊即失效**：granted 由收摊方 revoke（code 段关账 / loop 退出），
  不跨场复用。
- **申请超时自动拒**：惰性判（状态由时间戳算出来，不起定时器——重启也不漏拒）。
- **同一时刻至多一张待批单**：注入面反复怂恿也只会撞「已递待批」，Bark 不被刷屏。
- **串台纪律**（[[cassette-charswitch-bug-class]]）：状态按角色分文件
  （state/characters/<cid>/code_permit.json），没有全局「当前」。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Optional

import state_store

FILE_NAME = "code_permit.json"
REQUEST_TTL_SEC = 15 * 60      # 待批单超时自动拒（TA 不在场就当这次没批）
_LOCK = threading.Lock()


def _path(char_id: str):
    return state_store.char_state_dir(char_id) / FILE_NAME


def _load(char_id: str) -> dict:
    try:
        return json.loads(_path(char_id).read_text("utf-8"))
    except Exception:
        return {}


def _save(char_id: str, d: dict) -> None:
    p = _path(char_id)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _expire(d: dict) -> bool:
    """待批单过了 TTL → 自动拒（原地改，返回改没改）。"""
    p = d.get("pending")
    if p and time.time() - float(p.get("ts", 0)) > REQUEST_TTL_SEC:
        d["pending"] = None
        return True
    return False


def status(char_id: str) -> dict:
    """{"pending": {...}|None, "granted": {...}|None}。惰性清超时单。"""
    with _LOCK:
        d = _load(char_id)
        if _expire(d):
            _save(char_id, d)
        return {"pending": d.get("pending") or None,
                "granted": d.get("granted") or None}


def active(char_id: str) -> bool:
    """门问的唯一问题：这个角色现在有没有一份没收摊的写批准。"""
    return status(char_id)["granted"] is not None


def request(char_id: str, reason: str = "") -> dict:
    """递一张写权限申请（门在写类工具被拒时替他递）。幂等：已批 → granted；
    已有待批单 → 原样返回不重推 Bark（renewed=False）。"""
    with _LOCK:
        d = _load(char_id)
        _expire(d)
        if d.get("granted"):
            return {"state": "granted"}
        if d.get("pending"):
            return {"state": "pending", "renewed": False,
                    "id": d["pending"].get("id")}
        rec = {"id": uuid.uuid4().hex[:12], "reason": (reason or "").strip()[:200],
               "ts": int(time.time())}
        d["pending"] = rec
        _save(char_id, d)
    try:
        import characters
        from notify import bark_push
        who = characters.display_name(char_id)
        why = f"：{rec['reason']}" if rec["reason"] else ""
        bark_push(f"{who}想动手写代码{why}——到 app 里批（15 分钟内有效）",
                  title=who)
    except Exception:
        pass    # Bark 推不出去不影响状态：app 里照样看得到待批单
    return {"state": "pending", "renewed": True, "id": rec["id"]}


def decide(char_id: str, req_id: str, allow: bool) -> dict:
    """TA 拍板（app 弹窗/权限卡调）。对不上单号=可能已超时自动拒，有声报。"""
    with _LOCK:
        d = _load(char_id)
        _expire(d)
        p = d.get("pending")
        if not p or p.get("id") != req_id:
            return {"ok": False,
                    "error": "没有这张待批单（可能已超时自动拒了，让他再申请一次）"}
        d["pending"] = None
        if allow:
            d["granted"] = {**p, "granted_ts": int(time.time())}
        _save(char_id, d)
        return {"ok": True, "state": "granted" if allow else "denied"}


def revoke(char_id: str, why: str = "") -> None:
    """收摊即失效（一场一批）：granted/pending 一起清。幂等，失败不抛。"""
    try:
        with _LOCK:
            d = _load(char_id)
            if not d.get("granted") and not d.get("pending"):
                return
            d["granted"] = None
            d["pending"] = None
            _save(char_id, d)
        import sys
        print(f"[code_permits] 写批准收摊失效（char={char_id}，{why}）",
              file=sys.stderr)
    except Exception:
        pass
