"""手机推送（可选，Bark：https://github.com/Finb/Bark）。没配 BARK_URL 就静默跳过。"""
import sys
import urllib.request
from datetime import datetime
from urllib.parse import quote

import config


def logerr(msg: str) -> None:
    # 带时间戳：没时间戳的报错和几天前的老账分不清。
    ts = datetime.now().strftime("%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def bark_push_bg(text: str, title: str = "") -> None:
    """bark_push 的后台线程版：给事件循环上的调用点用（2026-09-12 体检）。
    bark_push 是同步 urlopen(timeout=5) 打公网——在 can_use_tool 回调/轮尾 finalize/
    收摊路径里直接调，Bark 抖动时整个后端冻 5s（所有角色的 SSE、弹卡回包、调度器一起停，
    STREAM_PING 心跳也发不出）。推送结果只进 stderr（失败照旧 logerr），调用方拿不到 ok。
    需要 ok 值的地方（wake_sdk 的 wake_log「bark」字段）仍用同步版，那是每次醒来一发、可接受。"""
    import threading
    threading.Thread(target=bark_push, args=(text, title), daemon=True,
                     name="bark-push").start()


def bark_push(text: str, title: str = "") -> bool:
    if not config.BARK_URL:
        return False
    try:
        # 标题=AI 的昵称（用户起的；多角色时调用方传各自的名字，不传=默认角色）；
        # 正文截断：中文 percent-encode 后一个字 9 字节，
        # 太长会撑爆 Bark 的 URI 上限（414）——通知只给提要，全文在 outbox 里、app 打开就有。
        url = f"{config.BARK_URL.rstrip('/')}/{quote(title or config.agent_name())}/{quote(text[:120])}"
        if config.BARK_ICON:
            url += "?icon=" + quote(config.BARK_ICON, safe="")
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        logerr(f"Bark 推送失败: {e}")
        return False
