"""activity_log：活动事件账本（PLAN_sdk §4「真相库扩容」，PR11 区间账 → PR13 升级）。

两层账，各答一个问题：

- **区间账**（activity_log.jsonl，PR11 原样）：「哪个角色的哪个场景从几点开到
  几点」——chat 重铸的文档框/折叠判据吃它。一行一个区间：
  {"char","scene","start","end","note"}。
- **事件账**（activity/segments/<seg_id>.jsonl，PR13 新增）：一场活动的逐条
  经历——点评/工具/截图引用，重铸「近 K 张图回填」和 30 天内的展开吃它。
  截图本体落 activity/shots/<seg_id>/，账里只存引用；30 天清（口径同
  uploads），章节志（笔记本）永存——之外的老段只能以折叠形态重铸（§4）。
- **行为账**（activity/acts-<char>.jsonl）：碰了外部世界的工具调用
  （读信/发信/写日记），开局包「行为清单」机械注入吃它（§5.4①，失约防御
  不靠检索）。wake 的 acts（PR12 wake_log 扩展字段）同时落这儿。

串台纪律：身份是每条记录/每个 seg_id 自带的字段，没有「当前活动」这种全局。
未收口段登记在 activity/open_segments.json——后端启动清扫据此补 close。
目录进 ops_check/备份纪律（§2.5 扩面：账本与 transcript 同在 Mac，自愈论证
对活动段失效，只能靠纪律）。
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import state_store

PATH = state_store.STATE_DIR / "activity_log.jsonl"
ACT_DIR = state_store.STATE_DIR / "activity"
SEG_DIR = ACT_DIR / "segments"
SHOT_DIR = ACT_DIR / "shots"
OPEN_PATH = ACT_DIR / "open_segments.json"
_LOCK = threading.Lock()

RETAIN_DAYS = 30          # 事件账/截图保留窗（§5.5 口径同 uploads）
EVENT_TEXT_CAP = 2000     # 大结果截断（§5.5：~2k/条）


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


# ---------- 事件账（PR13：每场一个 jsonl，截图存引用） ----------

def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", s)


def _mkdir700(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    try:
        p.chmod(0o700)
    except Exception:
        pass


def interval_seg_id(char_id: str, scene: str, start: int) -> str:
    """区间行 → 事件账地址。区间行不存 seg_id，但 open_segment 的构造是确定的
    （char-scene-start）——读取侧靠这个从折叠段找回它的事件账（S3 补线③）。
    和 open_segment 必须同源，别各拼各的。"""
    return f"{_safe_id(char_id)}-{_safe_id(scene)}-{int(start)}"


def open_segment(char_id: str, scene: str, start_ts: Optional[float] = None) -> str:
    """开一场：登记进 open_segments.json（启动清扫的账），返回 seg_id。
    身份钉进 seg_id，后续 append 不再需要 char/scene。目录 700（§2.5 扩面：
    账本与 transcript 同在 Mac，权限纪律同款）。"""
    start = int(start_ts or time.time())
    seg_id = interval_seg_id(char_id, scene, start)
    with _LOCK:
        _mkdir700(ACT_DIR)
        _mkdir700(SEG_DIR)
        opens = _read_opens()
        opens[seg_id] = {"char": char_id, "scene": scene, "start": start}
        _write_opens(opens)
    return seg_id


def append_event(seg_id: str, kind: str, **payload) -> None:
    """一场里的一条经历（comment/tool/shot/act…）。写失败只记不抛——账丢一条
    的代价是重铸时少一条材料，别影响正跑的轮。文本字段统一截 EVENT_TEXT_CAP。"""
    rec = {"ts": int(time.time()), "kind": kind}
    for k, v in payload.items():
        if isinstance(v, str) and len(v) > EVENT_TEXT_CAP:
            v = v[:EVENT_TEXT_CAP]
        rec[k] = v
    try:
        with _LOCK:
            SEG_DIR.mkdir(parents=True, exist_ok=True)
            with open(SEG_DIR / f"{_safe_id(seg_id)}.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        import sys
        print(f"[activity_log] 事件落账失败({seg_id}/{kind}): {e}", file=sys.stderr)


def save_shot(seg_id: str, jpg: bytes) -> Optional[str]:
    """截图本体落盘+账里记引用，返回引用路径（失败 None，不抛）。"""
    try:
        with _LOCK:
            d = SHOT_DIR / _safe_id(seg_id)
            _mkdir700(d)
            n = len(list(d.glob("*.jpg")))
            p = d / f"{n:05d}.jpg"
            p.write_bytes(jpg)
            p.chmod(0o600)
        append_event(seg_id, "shot", ref=str(p))
        return str(p)
    except Exception as e:
        import sys
        print(f"[activity_log] 截图落盘失败({seg_id}): {e}", file=sys.stderr)
        return None


def read_events(seg_id: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(SEG_DIR / f"{_safe_id(seg_id)}.jsonl", encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return out


def recent_shot_refs(seg_id: str, k: int) -> list[str]:
    """这一场最近 k 张还在盘上的截图引用（重铸「近 K 张图回填」的材料）。"""
    refs = [e.get("ref") for e in read_events(seg_id)
            if e.get("kind") == "shot" and e.get("ref")]
    return [r for r in refs if Path(r).exists()][-k:]


def close_segment(seg_id: str, note: str = "") -> None:
    """收一场：注销 open 登记 + 落区间行（chat 框/折叠判据的口粮，格式不动）。"""
    with _LOCK:
        opens = _read_opens()
        meta = opens.pop(seg_id, None)
        _write_opens(opens)
    if meta:
        append_interval(meta["char"], meta["scene"], meta["start"], note=note)


def open_segments() -> dict:
    """未收口的段（启动清扫/占用检查用）。{seg_id: {char,scene,start}}"""
    with _LOCK:
        return _read_opens()


def _read_opens() -> dict:
    try:
        return json.loads(OPEN_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _write_opens(d: dict) -> None:
    ACT_DIR.mkdir(exist_ok=True)
    tmp = OPEN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OPEN_PATH)


# ---------- 行为账（开局包「行为清单」的机械来源，§5.4①） ----------

def append_act(char_id: str, scene: str, tool: str, summary: str,
               ok: bool = True, ret: str = "") -> None:
    """碰了外部世界的工具调用留痕（判线 §5.2：对外部对象的读写都留；纯内部
    记忆操作不留）。执行层确定性落账，不靠他自觉。

    ⚠️ `ok` = **协议层没报错**，不是「事情办成了」——业务层的拒绝（配额用尽、
    对象不存在）在 `is_error` 上跟成功同形。「成没成」看 `ret`：执行层从
    `tool_result` 里抽的回执原话，不解读、不改判 `ok`。两列各答一个问题。"""
    rec = {"ts": int(time.time()), "scene": scene, "tool": tool,
           "ok": bool(ok), "text": (summary or "")[:200],
           "ret": (ret or "")[:200]}
    try:
        with _LOCK:
            ACT_DIR.mkdir(exist_ok=True)
            with open(ACT_DIR / f"acts-{_safe_id(char_id)}.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        import sys
        print(f"[activity_log] 行为落账失败({char_id}/{tool}): {e}", file=sys.stderr)


def recent_acts(char_id: str, since_ts: int = 0, limit: int = 20) -> list[dict]:
    out: list[dict] = []
    try:
        with open(ACT_DIR / f"acts-{_safe_id(char_id)}.jsonl",
                  encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if int(r.get("ts", 0)) > since_ts:
                    out.append(r)
    except FileNotFoundError:
        pass
    return out[-limit:]


# ---------- 保留窗清理（30 天，启动时跑一次） ----------

def cleanup(retain_days: int = RETAIN_DAYS, now: Optional[float] = None) -> int:
    """清事件账/截图里超保留窗的整场（按 seg_id 尾部的 start 时间戳判；开着的
    段绝不清）。行为账按行清。返回清掉的场数。区间账/章节志永存不归这儿。"""
    cutoff = int(now or time.time()) - retain_days * 86400
    removed = 0
    opens = set(open_segments().keys())
    for f in (list(SEG_DIR.glob("*.jsonl")) if SEG_DIR.exists() else []):
        seg_id = f.stem
        m = re.search(r"-(\d{9,})$", seg_id)
        if not m or seg_id in opens or int(m.group(1)) > cutoff:
            continue
        try:
            f.unlink()
            shutil.rmtree(SHOT_DIR / seg_id, ignore_errors=True)
            removed += 1
        except Exception as e:
            import sys
            print(f"[activity_log] 清理失败({seg_id}): {e}", file=sys.stderr)
    if ACT_DIR.exists():
        for f in ACT_DIR.glob("acts-*.jsonl"):
            try:
                keep = [l for l in f.read_text("utf-8").splitlines()
                        if (json.loads(l).get("ts", 0) if l.strip() else 0) > cutoff]
                f.write_text("\n".join(keep) + ("\n" if keep else ""),
                             encoding="utf-8")
            except Exception:
                continue
    return removed
