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

接线（app.py / wake.py）：看门狗线程每 2s 一拍 watchdog_tick()——Chrome（cassette
profile 那只）在跑而幽灵没在 → 搭伙；轮末 apply_choice() 按 [[browser:keep/close]]
标记结算：默认 release（Chrome 随最后一个客户端退出关闭）、keep=粘住、close=明确释放。
状态落盘 state/browser_keeper.json（sticky 重启不丢；会话本身要 ping 应答线程陪着，
后端重启后旧会话多半已被心跳收走，看门狗发现没人应答会自动重建）。

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

from notify import logerr

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "state" / "browser_keeper.json"
# 和插件转发壳同一个约定（cassette 的服务绑 IPv6 localhost，别写成 127.0.0.1）
MCP_URL = os.environ.get("CASSETTE_BROWSER_MCP_URL", "http://localhost:3002/mcp")
# pgrep 特征：Chromium 启动参数是 --user-data-dir=<dir>（等号连写），只有这只 Chrome 用这个
# profile；playwright-mcp 的 node 进程参数是 "--user-data-dir <dir>"（空格分开）匹配不上，
# 同机其它 profile 的 Chrome for Testing 也匹配不上。
# ⚠️ 模式不能以 "--" 开头——pgrep 会把它当自己的 flag 解析（实测），所以去掉前导横线。
PROFILE_ARG = "user-data-dir=" + str(BASE_DIR / "state" / "browser-profile")
RELEASE_COOLDOWN_SEC = 20   # release 后 Chrome 要几秒才真关，冷却期内看门狗别手欠再搭伙

_LOCK = threading.Lock()
_RESPONDER = {"sid": None, "thread": None}   # 当前 ping 应答线程守着哪个会话
# 禁代理：macOS urllib 吃系统代理，例外名单常只有 localhost 没有 127.0.0.1
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(msg: str) -> None:
    logerr(f"browser_keeper: {msg}")


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save(d: dict) -> None:
    # 原子写，临时名带唯一后缀（固定 .tmp 名并发写会撞车，state_store 同口径）
    tmp = STATE_PATH.with_name(f".browser_keeper.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    tmp.replace(STATE_PATH)


def _req(method: str, session_id: Optional[str] = None, body: Optional[dict] = None,
         timeout: float = 5, stream: bool = False):
    headers = {"Accept": "application/json, text/event-stream"}
    if stream:
        headers["Accept"] = "text/event-stream"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return _OPENER.open(urllib.request.Request(MCP_URL, data=data, headers=headers, method=method),
                        timeout=timeout)


def browser_running() -> bool:
    """持久 profile 那只 Chrome 在不在跑。"""
    try:
        r = subprocess.run(["pgrep", "-f", PROFILE_ARG], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def held() -> bool:
    st = _load()
    return bool(st.get("session_id")) and _responder_alive(st.get("session_id"))


def sticky() -> bool:
    return bool(_load().get("sticky"))


def _responder_alive(sid: Optional[str]) -> bool:
    t = _RESPONDER["thread"]
    return bool(sid) and _RESPONDER["sid"] == sid and t is not None and t.is_alive()


def _respond_ping(sid: str, req_id) -> None:
    with _req("POST", sid, {"jsonrpc": "2.0", "id": req_id, "result": {}}) as r:
        r.read()


def _responder_loop(sid: str, ready: threading.Event) -> None:
    """守着 GET SSE 长连接应答服务器的 ping（不应答会话 5s 就被收走）。
    流断了/会话没了就退出——看门狗发现 Chrome 还在跑会自动重建。
    ready：流真正连上后置位——ensure 必须等它再发工具调用，不然 backend 一建
    立即发出的第一个 ping 没有下行通道，5s 超时会话就被收走（实测踩过）。"""
    try:
        resp = _req("GET", sid, timeout=15, stream=True)   # ping 每 3s 一发，15s 静默=死
    except Exception as e:
        _log(f"GET 流打不开（{e}），应答线程退出")
        return
    finally:
        ready.set()   # 成败都放行 ensure（失败路径它的工具调用会自然暴露问题）
    try:
        buf: list = []
        for raw in resp:
            if _RESPONDER["sid"] != sid:
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
                    _respond_ping(sid, msg["id"])
                except Exception as e:
                    _log(f"回 ping 失败（{e}），应答线程退出")
                    break
    except Exception:
        pass   # 超时/流断，正常退出路径
    finally:
        try:
            resp.close()
        except Exception:
            pass
        _log(f"应答线程退出（{sid[:8]}…）")


def ensure(set_sticky: Optional[bool] = None) -> bool:
    """确保幽灵会话在且有人应答 ping。幂等；Chrome 没在跑就放弃（绝不新开窗口）；失败不抛。"""
    with _LOCK:
        st = _load()
        sid = st.get("session_id")
        if sid and _responder_alive(sid):
            if set_sticky is not None and st.get("sticky") != bool(set_sticky):
                st["sticky"] = bool(set_sticky)
                _save(st)
            return True
        if not browser_running():
            return False
        try:
            # ① initialize 握手
            with _req("POST", None, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "cassette-keeper", "version": "1.0"}},
            }) as r:
                new_sid = r.headers.get("mcp-session-id")
                r.read()
            if not new_sid:
                _log("initialize 没回 session id，放弃")
                return False
            with _req("POST", new_sid,
                      {"jsonrpc": "2.0", "method": "notifications/initialized"}) as r2:
                r2.read()
            # ② 先起 ping 应答线程，并等 GET 流真正连上（backend 一建心跳就开始，
            #    第一个 ping 没通道接就会 5s 超时收会话——顺序错了整个白搭）
            _RESPONDER["sid"] = new_sid
            ready = threading.Event()
            t = threading.Thread(target=_responder_loop, args=(new_sid, ready),
                                 daemon=True, name="browser-keeper-responder")
            _RESPONDER["thread"] = t
            t.start()
            ready.wait(timeout=5)
            # ③ 一次无害工具调用（browser_tabs list）：backend 惰性创建，
            #    这一下才真正把幽灵注册进 clientCount、搭上共享浏览器。
            with _req("POST", new_sid, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "browser_tabs", "arguments": {"action": "list"}},
            }, timeout=15) as r3:
                r3.read()
            _save({"session_id": new_sid,
                   "sticky": bool(set_sticky) if set_sticky is not None else bool(st.get("sticky"))})
            _log(f"幽灵会话已建立（{new_sid[:8]}…），Chrome 这轮结束不再自动关")
            return True
        except Exception as e:
            _log(f"ensure 失败: {e}")
            _RESPONDER["sid"] = None
            return False


def release() -> None:
    """释放幽灵会话：所有客户端都走后 Chrome 由 playwright-mcp 原生关闭。幂等。"""
    with _LOCK:
        st = _load()
        sid = st.get("session_id")
        _RESPONDER["sid"] = None   # 应答线程看到就退位
        if sid:
            try:
                with _req("DELETE", sid) as r:
                    r.read()
            except Exception:
                pass   # 会话可能已被心跳收走/服务重启，无妨
            _log(f"幽灵会话已释放（{sid[:8]}…）")
        _save({"session_id": None, "sticky": False, "released_at": int(time.time())})


def apply_choice(choice: Optional[str], browsed: bool) -> None:
    """轮末结算浏览器去留。默认（没标记）：这轮浏览过且没有粘性保留 → 释放幽灵（Chrome 照旧关）；
    keep → 幽灵粘住（看门狗多半已搭伙，这里补一手 + 置粘性）；close → 明确释放。
    没浏览也没标记的轮不碰 keeper——别让并行的 wake 轮被无关的 chat 轮误关。失败绝不拖垮轮。"""
    try:
        if choice == "close":
            release()
        elif choice == "keep":
            if not ensure(set_sticky=True):
                _log("keep 没成：Chrome 可能已经关了（这轮结束得太快），下次早点说")
        elif browsed and not sticky():
            release()
    except Exception as e:
        _log(f"apply_choice 失败: {e}")


def watchdog_tick() -> None:
    """看门狗一拍：Chrome 在跑而幽灵没真拿住（含后端重启后应答线程失踪）→ 搭伙。
    release 冷却期内不动（别拦住正常关闭）。"""
    st = _load()
    if st.get("session_id") and _responder_alive(st.get("session_id")):
        return
    if time.time() - float(st.get("released_at") or 0) < RELEASE_COOLDOWN_SEC:
        return
    if browser_running():
        if st.get("session_id"):
            # 有残留会话但没人应答（后端重启过）：多半已被心跳收走，直接重建
            _save({**st, "session_id": None})
        ensure()
