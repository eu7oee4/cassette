"""Ombre-Brain 的 REST 代理（记忆页 / 心流日志页的数据源）。

聊天走 MCP（pipeline.py）；这里是 Dashboard 同款 REST + cookie 鉴权。
app 不直连 Ombre——统一从 cassette 后端过：X-Auth 一道门，Ombre 密码只活在后端 .env。
写操作全走官方端点（/api/bucket/{id}/edit /forget），不需要给 Ombre 打任何补丁。
"""
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from fastapi import HTTPException

import config

_sess = {"cookie": "", "expiry": 0.0}
_LOCK = threading.Lock()

# macOS 系统代理例外名单常只有 localhost 没有 127.0.0.1——显式空代理，
# 同 pipeline.ombre_alive 口径，别让本机请求被梯子吞掉。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _login() -> str:
    """密码换 cookie。7 天有效，提前 1 天刷新。"""
    if not config.OMBRE_DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503,
                            detail="后端没配 OMBRE_DASHBOARD_PASSWORD，连不上记忆服务")
    req = urllib.request.Request(
        config.OMBRE_REST_URL + "/auth/login",
        data=json.dumps({"password": config.OMBRE_DASHBOARD_PASSWORD}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _opener.open(req, timeout=10) as r:
            for h in r.headers.get_all("Set-Cookie") or []:
                if h.startswith("ombre_session="):
                    _sess["cookie"] = h.split(";", 1)[0]
                    _sess["expiry"] = time.time() + 6 * 86400
                    return _sess["cookie"]
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Ombre 登录失败（{e.code}）")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"记忆服务不可达: {e}")
    raise HTTPException(status_code=502, detail="Ombre 登录没拿到会话 cookie")


def _cookie() -> str:
    with _LOCK:
        if not _sess["cookie"] or time.time() > _sess["expiry"]:
            return _login()
        return _sess["cookie"]


def call(path: str, body: Optional[dict] = None, retry: bool = True):
    """GET（body=None）或 POST 一个 Ombre REST 路径，返回解析后的 JSON。
    401 自动重登一次（cookie 过期 / Ombre 重启过）。"""
    ck = _cookie()
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Cookie": ck}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(config.OMBRE_REST_URL + path, data=data,
                                 headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with _opener.open(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            with _LOCK:
                _sess["cookie"] = ""
            return call(path, body, retry=False)
        raise HTTPException(status_code=502,
                            detail=f"Ombre {e.code}: {e.read().decode()[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"记忆服务不可达: {e}")
