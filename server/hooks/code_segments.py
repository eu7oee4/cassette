#!/usr/bin/env python3
"""
Code 会话的逐段上报 hook：把一轮里每个正文段落（工具调用之间说的话）单独 POST 回后端，
后端进 outbox，app 轮询上屏成气泡。

挂在 PostToolUse（说完一段去调工具的瞬间）+ Stop（收尾那段）两个事件上。
**只随 code 会话生效**——起会话时用 `claude --settings <这份配置>` 传进去，用户全局的
~/.claude/settings.json 一个字都不用改，他自己别的 claude 会话也不会触发这里。

工作方式：transcript(jsonl) 增量游标扫描——记住上次处理到第几行，只扫新增的、只发新段，
天然防重。

⚠️ 两处不能改的地方：
- transcript 是边写边读的。解析失败的**最后一行**多半是写了一半，绝不推进游标——
  当垃圾跳过会让整段话永远丢掉（实锤过）。
- Stop 时 transcript 常常还没把最后一段刷进盘（异步写），得等文件大小稳定；等完还没
  等到就用 stdin 里自带的 last_assistant_message 兜底（ts 相同，服务端会去重）。
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.request

TIMEOUT = 8       # hook 本身有超时，别在这儿耗着
STABLE_TRIES = 12  # Stop 时等 transcript 落盘：最多 ~4s


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    url = (os.environ.get("CASSETTE_BACKEND_URL") or "").rstrip("/")
    token = os.environ.get("CASSETTE_AUTH_KEY") or ""
    if not url or not token:
        return   # 不是我们起的会话（或 env 没传下来）：安静退出，别乱发

    path = payload.get("transcript_path") or ""
    if not path or not os.path.exists(path):
        return
    is_stop = (payload.get("hook_event_name") or "") == "Stop"

    if is_stop:
        last_size, stable = -1, 0
        for _ in range(STABLE_TRIES):
            time.sleep(0.35)
            try:
                size = os.path.getsize(path)
            except OSError:
                break
            if size == last_size:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last_size = size

    session = (payload.get("session_id") or "nosid")[:16]
    cursor_path = os.path.join(tempfile.gettempdir(), f"cassette_code_seg_{session}")
    try:
        with open(cursor_path) as f:
            done = int(f.read().strip())
    except Exception:
        done = 0

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    if done > len(lines):
        done = 0   # 文件比游标还短 = 换了/清了 transcript，从头扫（去重兜着，不会重发）

    def post(body: str) -> bool:
        data = json.dumps({
            "role": "assistant",
            "text": body,
            # Stop = 这一轮说完了（TA 在等人）；PostToolUse = 中间过程。
            # 后端只对 -stop 推 Bark：干一小时活的中间段落全推会有上百条通知。
            "source": "code-stop" if is_stop else "code-seg",
            # ts 用正文哈希派生：游标扫描和 Stop 兜底两条路发同一段话时 ts 一致，
            # 服务端 (ts + 正文 md5) 去重才稳（hook 超时重试也是同一个 ts）。
            "ts": "seg-" + hashlib.md5(body.encode("utf-8")).hexdigest()[:16],
        }).encode("utf-8")
        req = urllib.request.Request(url + "/code/append", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Auth", token)
        try:
            # 显式空代理：系统代理的例外名单常常只写了 localhost 没写 127.0.0.1，
            # 走代理会把本机请求吞掉，还查不出原因。
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=TIMEOUT) as resp:
                return resp.status == 200
        except Exception:
            return False

    advanced = done
    sent: list = []
    for i in range(done, len(lines)):
        raw = lines[i].strip()
        if not raw:
            advanced = i + 1
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            # 写了一半的最后一行：停在这儿，等下个事件它写完了再扫。绝不当垃圾跳过。
            if i >= len(lines) - 1:
                break
            advanced = i + 1
            continue
        texts = []
        if obj.get("type") == "assistant":
            for c in (obj.get("message", {}).get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip():
                    texts.append(c["text"].strip())
        if not texts:
            advanced = i + 1
            continue
        body = "\n\n".join(texts)
        if not post(body):
            break            # 没发出去就不推进游标，下个事件重试
        advanced = i + 1
        sent.append(body)

    try:
        with open(cursor_path, "w") as f:
            f.write(str(advanced))
    except OSError:
        pass

    # Stop 兜底：等了稳定期 transcript 还是没把最后一段落盘的话，stdin 里带着权威的
    # last_assistant_message——直接发它。之后游标扫到同一段，ts 相同会被服务端去重。
    if is_stop:
        last = (payload.get("last_assistant_message") or "").strip()
        if last and last not in sent:
            post(last)


if __name__ == "__main__":
    main()
