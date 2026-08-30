"""activity_log：活动区间账（PLAN_sdk §4「真相库扩容」的最小雏形，PR11 落地）。

现在只记「哪个角色的哪个场景从几点开到几点」——chat 重铸时用它给回流的
游戏点评加两行文档框（§4 规则二）。PR13 升级成逐条事件账本（每场 jsonl、
进 ops_check/备份纪律），这个文件是它的接口先声。

append-only jsonl，一行一个区间：{"char","scene","start","end","note"}。
串台纪律：身份是每条记录自带的字段，没有「当前活动」这种全局。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Optional

import state_store

PATH = state_store.STATE_DIR / "activity_log.jsonl"
_LOCK = threading.Lock()


def append_interval(char_id: str, scene: str, start_ts: float,
                    end_ts: Optional[float] = None, note: str = "") -> None:
    """收摊时落一条。写失败只记不抛——账丢一条的代价是那段点评少个框，别影响收摊。"""
    rec = {"char": char_id, "scene": scene,
           "start": int(start_ts), "end": int(end_ts or time.time()),
           "note": note}
    try:
        with _LOCK:
            with open(PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        import sys
        print(f"[activity_log] 落账失败: {e}", file=sys.stderr)


def read_intervals(char_id: str, scene: Optional[str] = None,
                   since_ts: int = 0) -> list[dict]:
    """该角色的区间，按 start 升序。since_ts=只要结束时间在其后的（窗口相关的才有用）。"""
    out: list[dict] = []
    try:
        with open(PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("char") != char_id:
                    continue
                if scene and r.get("scene") != scene:
                    continue
                if int(r.get("end", 0)) <= since_ts:
                    continue
                out.append(r)
    except FileNotFoundError:
        pass
    except Exception as e:
        import sys
        print(f"[activity_log] 读账失败: {e}", file=sys.stderr)
    out.sort(key=lambda r: int(r.get("start", 0)))
    return out
