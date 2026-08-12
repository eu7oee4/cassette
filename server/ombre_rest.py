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

import characters

_sessions: dict[str, dict] = {}   # rest_url → {"cookie", "expiry"}；每个角色可指不同实例
_LOCK = threading.Lock()

# macOS 系统代理例外名单常只有 localhost 没有 127.0.0.1——显式空代理，
# 同 pipeline.ombre_alive 口径，别让本机请求被梯子吞掉。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _login(oc: dict) -> str:
    """密码换 cookie。7 天有效，提前 1 天刷新。会话按 rest_url 各存各的。"""
    if not oc["dashboard_password"]:
        raise HTTPException(status_code=503,
                            detail="这个角色没配 Ombre 的 dashboard_password，连不上记忆服务")
    req = urllib.request.Request(
        oc["rest_url"] + "/auth/login",
        data=json.dumps({"password": oc["dashboard_password"]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    sess = _sessions.setdefault(oc["rest_url"], {"cookie": "", "expiry": 0.0})
    try:
        with _opener.open(req, timeout=10) as r:
            for h in r.headers.get_all("Set-Cookie") or []:
                if h.startswith("ombre_session="):
                    sess["cookie"] = h.split(";", 1)[0]
                    sess["expiry"] = time.time() + 6 * 86400
                    return sess["cookie"]
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Ombre 登录失败（{e.code}）")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"记忆服务不可达: {e}")
    raise HTTPException(status_code=502, detail="Ombre 登录没拿到会话 cookie")


def _cookie(oc: dict) -> str:
    with _LOCK:
        sess = _sessions.setdefault(oc["rest_url"], {"cookie": "", "expiry": 0.0})
        if not sess["cookie"] or time.time() > sess["expiry"]:
            return _login(oc)
        return sess["cookie"]


def call(path: str, body: Optional[dict] = None, retry: bool = True,
         method: Optional[str] = None, char_id: Optional[str] = None):
    """调一个角色的 Ombre REST 路径，返回解析后的 JSON。401 自动重登一次（cookie 过期 /
    Ombre 重启过）。method 不传时按 body 推断：有 body → POST，无 body → GET；
    显式传（如 DELETE）时用它，DELETE 也可以不带 body（归档删除走 ?confirm=true 的 query）。"""
    oc = characters.ombre_conf(char_id)
    ck = _cookie(oc)
    data = json.dumps(body).encode() if body is not None else None
    m = method or ("POST" if data is not None else "GET")
    headers = {"Cookie": ck}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(oc["rest_url"] + path, data=data,
                                 headers=headers, method=m)
    try:
        with _opener.open(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401 and retry:
            with _LOCK:
                _sessions.setdefault(oc["rest_url"], {"cookie": "", "expiry": 0.0})["cookie"] = ""
            return call(path, body, retry=False, method=method, char_id=char_id)
        raise HTTPException(status_code=502,
                            detail=f"Ombre {e.code}: {e.read().decode()[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"记忆服务不可达: {e}")
