#!/usr/bin/env python3
"""
Code 会话的逐段上报 hook：把一轮里每个正文段落（工具调用之间说的话）单独 POST 回后端，
后端进 outbox，app 轮询上屏成气泡。

挂在 PostToolUse（说完一段去调工具的瞬间）+ Stop（收尾那段）两个事件上。
**只随 code 会话生效**——起会话时用 `claude --settings <这份配置>` 传进去，用户全局的
~/.claude/settings.json 一个字都不用改，他自己别的 claude 会话也不会触发这里。

工作方式：transcript(jsonl) 增量游标扫描——记住上次处理到第几行，只扫新增的、只发新段，
天然防重。

⚠️ 三处不能改的地方：
- transcript 是边写边读的。解析失败的**最后一行**多半是写了一半，绝不推进游标——
  当垃圾跳过会让整段话永远丢掉（实锤过）。
- Stop 时 transcript 常常还没把最后一段刷进盘（异步写），得等文件大小稳定；等完还没
  等到就用 stdin 里自带的 last_assistant_message 兜底。
- 兜底发过的正文要记在状态文件里（posted），游标后来扫到同一段就只推进、不再发。
  **别改回"两条路都用正文哈希当 id、靠服务端撞键去重"**：那样两段一模一样的正文
  （"汪。"这种）会被认成同一段，第二次直接被吞掉，人在手机上什么都看不到。
"""
import hashlib
import json
import os
import sys
import tempfile
import time
import urllib.request

TIMEOUT = 8        # hook 本身有超时，别在这儿耗着
STABLE_TRIES = 12  # Stop 时等 transcript 落盘：最多 ~4s
POSTED_KEEP = 40   # 状态文件里留多少条"兜底发过"的哈希（只用来挡游标重发，不用留全）


def _hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


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
    # 状态文件：{"cursor": 扫到第几行, "posted": [兜底发过的正文哈希]}。
    # 旧版是个裸整数（只有游标），读到就当 cursor 用——换版本时正在跑的会话不会重发。
    done, posted = 0, []
    try:
        with open(cursor_path) as f:
            raw_state = f.read().strip()
        try:
            st = json.loads(raw_state)
            done = int(st.get("cursor") or 0)
            posted = [h for h in (st.get("posted") or []) if isinstance(h, str)]
        except Exception:
            done = int(raw_state)
    except Exception:
        done, posted = 0, []

    def save_state() -> None:
        try:
            with open(cursor_path, "w") as f:
                json.dump({"cursor": advanced, "posted": posted[-POSTED_KEEP:]}, f)
        except OSError:
            pass

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    if done > len(lines):
        done = 0   # 文件比游标还短 = 换了/清了 transcript，从头扫（去重兜着，不会重发）

    def post(body: str, seg_id: str) -> bool:
        data = json.dumps({
            "role": "assistant",
            "text": body,
            # Stop = 这一轮说完了（TA 在等人）；PostToolUse = 中间过程。
            # 后端只对 -stop 推 Bark：干一小时活的中间段落全推会有上百条通知。
            "source": "code-stop" if is_stop else "code-seg",
            # ts = 这一段的身份，服务端拿 (ts + 正文 md5) 去重。
            # 游标扫描用 transcript 那行的 uuid：**同一段重发是同一个 id，两段碰巧
            # 一模一样的正文是不同 id**——hook 超时重试照样去得掉，正文撞车不会误杀。
            "ts": seg_id,
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
        if _hash(body) in posted:
            # 这段上一轮 Stop 已经兜底发过了（那会儿 transcript 还没落盘）。跳过，只推进游标。
            # 对上就把它从 posted 划掉（一条挡一次）：不然之后再说一句一模一样的话，
            # 会被这条陈年记录一直挡着，永远上不了屏。
            posted.remove(_hash(body))
            advanced = i + 1
            continue
        # 身份优先用这一行的 uuid（每行唯一、重发不变）；万一没有再退回正文哈希。
        if not post(body, "seg-" + (obj.get("uuid") or _hash(body))):
            break            # 没发出去就不推进游标，下个事件重试
        advanced = i + 1
        sent.append(body)

    save_state()

    # Stop 兜底：等了稳定期 transcript 还是没把最后一段落盘的话，stdin 里带着权威的
    # last_assistant_message——直接发它。它没有 uuid（stdin 里不带），只能用正文哈希当身份；
    # 记进 posted，游标之后扫到这一行就不会再发一遍。
    if is_stop:
        last = (payload.get("last_assistant_message") or "").strip()
        if last and last not in sent and _hash(last) not in posted:
            if post(last, "seg-" + _hash(last)):
                posted.append(_hash(last))
                save_state()


if __name__ == "__main__":
    main()
