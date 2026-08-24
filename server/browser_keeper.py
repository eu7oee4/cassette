"""幽灵会话（browser keeper）：让 TA 的 Chrome 在 claude 轮结束后按 TA 的意愿活下来。

@playwright/mcp --shared-browser-context 的关闭机制（v0.0.78 coreBundle 实读，
插件仓钉的就是这版；**升 playwright 版本要回来重验这段**）：
- backend（占一个 clientCount）在会话**首次工具调用**时才惰性创建；
- backend 建好后服务器每 3s 反向 ping 客户端（server.ping），5s 没回应就关会话；
- 会话关闭 → dispose → clientCount 归零时连浏览器一起关。
claude -p 退出后插件转发壳跟着死、GET 流断、ping 无人应答 → 会话几秒内被关
——这就是"轮末 Chrome 自动关"。

所以幽灵要占住浏览器得做全三件事：
① initialize 握手拿 session id；② 开着 GET SSE 长连接、线程应答 ping；
③ 发一次无害工具调用（browser_tabs list）把 backend 注册进 clientCount。
release() 发 DELETE，走它原生 dispose，干净关闭。

**一人一个浏览器**（2026-08-24，照邮箱 2026-08-15 的成例）：浏览器是带登录态的身份
不是设备。每个角色一套 playwright-mcp 服务（端口、--user-data-dir 各一份，launchd
label 带角色名），接线走 characters.browser_conf（默认角色零配置=旧 3002 行为；
其他角色必须在 char.json 写 browser.mcp_url，没配 = 没有自己的浏览器，keeper 跳过、
插件也不挂载——见 plugins.PLUGIN_GATE，别让谁静默落到别人的浏览器上）。
状态各落 state/characters/<id>/：browser-profile/（登录态）+ browser_keeper.json。
runtime（node 包 + Chromium 二进制）保持全局一份 state/browser-runtime/——
那是设备不是身份，jobhunt 渲 PDF 也蹭着它。

接线（app.py / wake.py）：看门狗线程每 2s 逐角色一拍 watchdog_tick(cid)——Chrome
（那个角色 profile 的那只）在跑而幽灵没在 → 搭伙；轮末 apply_choice() 按
[[browser:keep/close]] 标记结算：默认 release、keep=粘住、close=明确释放。
sticky 落盘重启不丢；会话本身要 ping 应答线程陪着，后端重启后旧会话多半已被
心跳收走，看门狗发现没人应答会自动重建。

纪律：绝不主动拉起浏览器（Chrome 没在跑时 ensure 直接放弃——不然工具调用会新开窗口）；
所有对外函数不抛异常（浏览器体系的口径：挂了就当没有，绝不拖垮 chat/wake）。"""
import json
import os
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

import state_store
from notify import logerr

RELEASE_COOLDOWN_SEC = 20   # release 后 Chrome 要几秒才真关，冷却期内看门狗别手欠再搭伙

_LOCK = threading.Lock()
# 每个角色一条 ping 应答线程：cid -> {"sid": ..., "thread": ...}
_RESPONDER: dict[str, dict] = {}
# 禁代理：macOS urllib 吃系统代理，例外名单常只有 localhost 没有 127.0.0.1
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _cid(char_id=None) -> str:
    return char_id or state_store.DEFAULT_CHAR_ID


def _log(msg: str, char_id=None) -> None:
    logerr(f"browser_keeper[{_cid(char_id)}]: {msg}")


# ---------- 每个角色一套的接线与路径 ----------

def mcp_url(char_id=None) -> str:
    """这个角色的浏览器服务地址（characters.browser_conf）。空串 = 这个角色没有
    自己的浏览器。（cassette 的服务绑 IPv6 localhost，别写成 127.0.0.1。）"""
    import characters   # 函数内 import：characters → state_store/config，避免模块级环
    return characters.browser_conf(_cid(char_id))["MCP_URL"]


def configured(char_id=None) -> bool:
    return bool(mcp_url(char_id))


def _profile_dir(char_id=None) -> Path:
    return state_store.char_state_dir(_cid(char_id)) / "browser-profile"


def _profile_arg(char_id=None) -> str:
    # pgrep 特征：Chromium 启动参数是 --user-data-dir=<dir>（等号连写），只有这个角色
    # 的 Chrome 用这个 profile；playwright-mcp 的 node 进程参数是 "--user-data-dir <dir>"
    # （空格分开）匹配不上，同机其它 profile 的 Chrome for Testing 也匹配不上。
    # ⚠️ 模式不能以 "--" 开头——pgrep 会把它当自己的 flag 解析（实测），所以去掉前导横线。
    return "user-data-dir=" + str(_profile_dir(char_id))


def _state_path(char_id=None) -> Path:
    return state_store.char_state_dir(_cid(char_id)) / "browser_keeper.json"


def _load(char_id=None) -> dict:
    try:
        return json.loads(_state_path(char_id).read_text())
    except Exception:
        return {}


def _save(d: dict, char_id=None) -> None:
    # 原子写，临时名带唯一后缀（固定 .tmp 名并发写会撞车，state_store 同口径）
    path = _state_path(char_id)
    tmp = path.with_name(f".browser_keeper.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    tmp.replace(path)


def _req(method: str, session_id: Optional[str] = None, body: Optional[dict] = None,
         timeout: float = 5, stream: bool = False, char_id=None):
    headers = {"Accept": "application/json, text/event-stream"}
    if stream:
        headers["Accept"] = "text/event-stream"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return _OPENER.open(urllib.request.Request(mcp_url(char_id), data=data,
                                               headers=headers, method=method),
                        timeout=timeout)


def browser_running(char_id=None) -> bool:
    """这个角色持久 profile 那只 Chrome 在不在跑。"""
    try:
        r = subprocess.run(["pgrep", "-f", _profile_arg(char_id)],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def held(char_id=None) -> bool:
    st = _load(char_id)
    return bool(st.get("session_id")) and _responder_alive(st.get("session_id"), char_id)


def sticky(char_id=None) -> bool:
    return bool(_load(char_id).get("sticky"))


def _responder(char_id=None) -> dict:
    return _RESPONDER.setdefault(_cid(char_id), {"sid": None, "thread": None})


def _responder_alive(sid: Optional[str], char_id=None) -> bool:
    r = _responder(char_id)
    t = r["thread"]
    return bool(sid) and r["sid"] == sid and t is not None and t.is_alive()


def _respond_ping(sid: str, req_id, char_id=None) -> None:
    with _req("POST", sid, {"jsonrpc": "2.0", "id": req_id, "result": {}},
              char_id=char_id) as r:
        r.read()


def _responder_loop(sid: str, ready: threading.Event, char_id=None) -> None:
    """守着 GET SSE 长连接应答服务器的 ping（不应答会话 5s 就被收走）。
    流断了/会话没了就退出——看门狗发现 Chrome 还在跑会自动重建。
    ready：流真正连上后置位——ensure 必须等它再发工具调用，不然 backend 一建
    立即发出的第一个 ping 没有下行通道，5s 超时会话就被收走（实测踩过）。"""
    try:
        resp = _req("GET", sid, timeout=15, stream=True, char_id=char_id)   # ping 每 3s 一发，15s 静默=死
    except Exception as e:
        _log(f"GET 流打不开（{e}），应答线程退出", char_id)
        return
    finally:
        ready.set()   # 成败都放行 ensure（失败路径它的工具调用会自然暴露问题）
    try:
        buf: list = []
        for raw in resp:
            if _responder(char_id)["sid"] != sid:
                break   # 已换新会话/已释放，老线程退位
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("data:"):
                buf.append(line[5:].strip())
                continue
            if line:
                continue   # event:/id: 等字段，不关心
            if not buf:
                continue
            payload = "".join(buf)
            buf = []
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if msg.get("method") == "ping" and msg.get("id") is not None:
                try:
                    _respond_ping(sid, msg["id"], char_id)
                except Exception as e:
                    _log(f"回 ping 失败（{e}），应答线程退出", char_id)
                    break
    except Exception:
        pass   # 超时/流断，正常退出路径
    finally:
        try:
            resp.close()
        except Exception:
            pass
        _log(f"应答线程退出（{sid[:8]}…）", char_id)


def ensure(set_sticky: Optional[bool] = None, char_id=None) -> bool:
    """确保幽灵会话在且有人应答 ping。幂等；Chrome 没在跑就放弃（绝不新开窗口）；失败不抛。"""
    if not configured(char_id):
        return False
    with _LOCK:
        st = _load(char_id)
        sid = st.get("session_id")
        if sid and _responder_alive(sid, char_id):
            if set_sticky is not None and st.get("sticky") != bool(set_sticky):
                st["sticky"] = bool(set_sticky)
                _save(st, char_id)
            return True
        if not browser_running(char_id):
            return False
        try:
            # ① initialize 握手
            with _req("POST", None, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "cassette-keeper", "version": "1.0"}},
            }, char_id=char_id) as r:
                new_sid = r.headers.get("mcp-session-id")
                r.read()
            if not new_sid:
                _log("initialize 没回 session id，放弃", char_id)
                return False
            with _req("POST", new_sid,
                      {"jsonrpc": "2.0", "method": "notifications/initialized"},
                      char_id=char_id) as r2:
                r2.read()
            # ② 先起 ping 应答线程，并等 GET 流真正连上（backend 一建心跳就开始，
            #    第一个 ping 没通道接就会 5s 超时收会话——顺序错了整个白搭）
            resp_slot = _responder(char_id)
            resp_slot["sid"] = new_sid
            ready = threading.Event()
            t = threading.Thread(target=_responder_loop, args=(new_sid, ready, _cid(char_id)),
                                 daemon=True,
                                 name=f"browser-keeper-responder-{_cid(char_id)}")
            resp_slot["thread"] = t
            t.start()
            ready.wait(timeout=5)
            # ③ 一次无害工具调用（browser_tabs list）：backend 惰性创建，
            #    这一下才真正把幽灵注册进 clientCount、搭上共享浏览器。
            with _req("POST", new_sid, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "browser_tabs", "arguments": {"action": "list"}},
            }, timeout=15, char_id=char_id) as r3:
                r3.read()
            _save({"session_id": new_sid,
                   "sticky": bool(set_sticky) if set_sticky is not None else bool(st.get("sticky"))},
                  char_id)
            _log(f"幽灵会话已建立（{new_sid[:8]}…），Chrome 这轮结束不再自动关", char_id)
            return True
        except Exception as e:
            _log(f"ensure 失败: {e}", char_id)
            _responder(char_id)["sid"] = None
            return False


def release(char_id=None) -> None:
    """释放幽灵会话：所有客户端都走后 Chrome 由 playwright-mcp 原生关闭。幂等。"""
    with _LOCK:
        st = _load(char_id)
        sid = st.get("session_id")
        _responder(char_id)["sid"] = None   # 应答线程看到就退位
        if sid:
            try:
                with _req("DELETE", sid, char_id=char_id) as r:
                    r.read()
            except Exception:
                pass   # 会话可能已被心跳收走/服务重启，无妨
            _log(f"幽灵会话已释放（{sid[:8]}…）", char_id)
        _save({"session_id": None, "sticky": False, "released_at": int(time.time())}, char_id)


def apply_choice(choice: Optional[str], browsed: bool, char_id=None) -> None:
    """轮末结算浏览器去留。默认（没标记）：这轮浏览过且没有粘性保留 → 释放幽灵（Chrome 照旧关）；
    keep → 幽灵粘住（看门狗多半已搭伙，这里补一手 + 置粘性）；close → 明确释放。
    没浏览也没标记的轮不碰 keeper——别让并行的 wake 轮被无关的 chat 轮误关。
    一人一个之后这里按角色结算：A 关的是 A 自己的窗口，误关别人的那类事故从结构上没了。
    失败绝不拖垮轮。"""
    try:
        if not configured(char_id):
            return
        if choice == "close":
            release(char_id)
        elif choice == "keep":
            if not ensure(set_sticky=True, char_id=char_id):
                _log("keep 没成：Chrome 可能已经关了（这轮结束得太快），下次早点说", char_id)
        elif browsed and not sticky(char_id):
            release(char_id)
    except Exception as e:
        _log(f"apply_choice 失败: {e}", char_id)


def watchdog_tick(char_id=None) -> None:
    """看门狗一拍：Chrome 在跑而幽灵没真拿住（含后端重启后应答线程失踪）→ 搭伙。
    release 冷却期内不动（别拦住正常关闭）。调用方逐角色拍（app._browser_keeper_watchdog）。"""
    st = _load(char_id)
    if st.get("session_id") and _responder_alive(st.get("session_id"), char_id):
        return
    if time.time() - float(st.get("released_at") or 0) < RELEASE_COOLDOWN_SEC:
        return
    if browser_running(char_id):
        if st.get("session_id"):
            # 有残留会话但没人应答（后端重启过）：多半已被心跳收走，直接重建
            _save({**st, "session_id": None}, char_id)
        ensure(char_id=char_id)
