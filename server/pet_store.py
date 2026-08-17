"""宠物状态 + PetLog 存储（PLAN_pet P1，从 mianmian pet_store 搬家改造）。

每宠物一目录（state/ 下，gitignore）：
    state/pets/<pid>/state.json     四值 + 猫砂盆 + 睡眠 + 憋屎压力（原子写）
    state/pets/<pid>/petlog.jsonl   照料记录 append-only（actor = 实体 id | "system"）

对 mianmian 版的改造点：
- **poke 摘除**：薅醒物理化了——想找人自己走过去挠门（需求系统 + P2 触发），
  不再靠一个标记戳 wake 轮询；
- **新增 poop_pressure（憋屎压力 0-100）**：mianmian 的教训是铲屎形同摆设，因为
  从没让模型拉屎——生存性行为不靠模型自觉。压力随时间涨、吃东西加速，到阈值
  变成需求（needs()），过硬上限由引擎强制执行（pet_engine.enforce）；
- **位置不在这里**：world.json 是位置权威，pet 是普通实体（PLAN_pet P0）；
- needs()：从状态推导当前需求清单，P2 的猫 tick 拿它当醒因，prompt 拿它当感受。

衰减照旧惰性算：只存 baseline + updated_at，读时按流逝小时现算，无定时任务。
睡着时四项衰减和憋屎都暂停、精力恢复、够了自醒（猫的日子过得很简单）。
"""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import state_store

PETS_STATE_ROOT = state_store.STATE_DIR / "pets"

# ---- 衰减/恢复速率（点/小时），口径同 mianmian ----
SATIETY_DECAY = 5.0      # 饱腹：主照料项，掉得最快（~20h 掉满）
HYDRATION_DECAY = 3.5    # 水分：中速
ENERGY_DECAY = 6.0       # 精力：醒着掉
ENERGY_RECOVER = 20.0    # 精力：睡着恢复（~5h 回满）
AUTO_WAKE_ENERGY = 90.0  # 睡到精力回到这个值 → 自醒

# ---- 憋屎压力（新增）----
POOP_RATE = 9.0          # 点/小时（醒着才涨，~11h 涨满）
POOP_MEAL_BOOST = 12.0   # 吃了东西加速（引擎在投喂后调 add_poop_pressure）
POOP_WANT_AT = 60.0      # 到这里成为需求（想拉屎 → 该去猫房了）
POOP_FORCE_AT = 95.0     # 过这里引擎强制执行（pet_engine.enforce）

# ---- 需求阈值 ----
HUNGRY_AT = 35.0         # 饿：讨罐罐 / 自己去吃猫粮
THIRSTY_AT = 35.0        # 渴：猫房饮水机
SLEEPY_AT = 20.0         # 困：找地方睡
FORCE_EAT_AT = 10.0      # 硬地板：自动喂食器的兜底语义——不靠模型自觉也饿不死

STAT_KEYS = ("satiety", "hydration", "energy", "mood")
LITTER_CN = {1: "干净", 2: "有屎", 3: "满了"}


def _now() -> float:
    return time.time()


def _clamp(v, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, float(v)))


def _dir(pid: str) -> Path:
    return PETS_STATE_ROOT / pid


def default_state() -> dict:
    now = _now()
    return {
        "satiety": 80.0,
        "hydration": 70.0,
        "energy": 70.0,
        "mood": 65.0,
        "litter": 1,                    # 1 干净 / 2 有屎 / 3 满了
        "asleep": False,
        "poop_pressure": 0.0,
        "pose": None,                   # 姿态快照锚点 {"room", "entry_id"}（引擎维护）
        "created_at": now,
        "updated_at": now,
    }


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def _load_raw(pid: str) -> dict:
    s = default_state()
    try:
        s.update(json.loads((_dir(pid) / "state.json").read_text("utf-8")))
    except Exception:
        pass
    s.pop("poke", None)   # 搬家来的老状态可能带 poke——摘除，物理化薅醒取代
    return s


def _apply_decay(s: dict, now: float) -> dict:
    hours = max(0.0, (now - float(s.get("updated_at", now))) / 3600.0)
    if hours <= 0:
        return s
    if s.get("asleep"):
        s["energy"] = _clamp(s["energy"] + ENERGY_RECOVER * hours)
        if s["energy"] >= AUTO_WAKE_ENERGY:
            s["asleep"] = False   # 睡够自醒
    else:
        s["satiety"] = _clamp(s["satiety"] - SATIETY_DECAY * hours)
        s["hydration"] = _clamp(s["hydration"] - HYDRATION_DECAY * hours)
        s["energy"] = _clamp(s["energy"] - ENERGY_DECAY * hours)
        s["poop_pressure"] = _clamp(float(s.get("poop_pressure", 0)) + POOP_RATE * hours)
    return s


def _with_derived(s: dict) -> dict:
    out = dict(s)
    out["day"] = int((_now() - float(s.get("created_at", _now()))) // 86400) + 1
    for k in STAT_KEYS:
        out[k] = round(float(out[k]))
    return out


def read_state(pid: str) -> dict:
    """当前状态（已算衰减，含 day）。只读、不落盘。"""
    return _with_derived(_apply_decay(_load_raw(pid), _now()))


def mutate(pid: str, fn: Callable[[dict], None]) -> dict:
    """读 baseline → 物化衰减 → fn 就地改 → 落盘。返回 read_state 形态。"""
    now = _now()
    s = _apply_decay(_load_raw(pid), now)
    fn(s)
    for k in STAT_KEYS:
        s[k] = _clamp(s[k])
    s["litter"] = max(1, min(3, int(s.get("litter", 1))))
    s["poop_pressure"] = _clamp(float(s.get("poop_pressure", 0)))
    s["updated_at"] = now
    _write_json(_dir(pid) / "state.json", s)
    return _with_derived(s)


# ---------- 具体改动 ----------
def apply_interaction(pid: str, new_stats: Optional[dict] = None,
                      litter: Optional[int] = None,
                      asleep: Optional[bool] = None,
                      poop_add: float = 0.0,
                      poop_reset: bool = False) -> dict:
    """模型输出应用：new_stats 是**新的绝对值**（四条任选）；litter/asleep 可选；
    poop_add=吃了东西加压，poop_reset=拉完清零（引擎的确定性覆盖调这里）。"""
    def fn(s: dict) -> None:
        for k in STAT_KEYS:
            if new_stats and new_stats.get(k) is not None:
                s[k] = _clamp(new_stats[k])
        if litter is not None:
            s["litter"] = litter
        if asleep is not None:
            s["asleep"] = bool(asleep)
        if poop_reset:
            s["poop_pressure"] = 0.0
        elif poop_add:
            s["poop_pressure"] = _clamp(float(s.get("poop_pressure", 0)) + poop_add)
    return mutate(pid, fn)


def set_mood(pid: str, value: float) -> dict:
    """机主手滑心情（P3 UI）。"""
    return mutate(pid, lambda s: s.__setitem__("mood", _clamp(value)))


def scoop(pid: str) -> dict:
    """铲屎 → 猫砂盆置回 1 干净。"""
    return mutate(pid, lambda s: s.__setitem__("litter", 1))


def set_pose_anchor(pid: str, anchor: Optional[dict]) -> None:
    """姿态快照锚点（引擎维护：{"room", "entry_id"} 或 None）。不走 mutate——
    锚点不是猫的身体状态，不该顺手物化衰减。"""
    s = _load_raw(pid)
    s["pose"] = anchor
    _write_json(_dir(pid) / "state.json", s)


def needs(state: dict) -> list[dict]:
    """从状态推导需求清单：P2 猫 tick 的醒因、prompt 里的感受。每条
    {kind, text}——text 用第二人称写给猫自己。"""
    out = []
    if state["satiety"] < HUNGRY_AT:
        out.append({"kind": "hungry",
                    "text": "你饿了——去猫房吃口猫粮，或者找个人讨罐罐"})
    if state["hydration"] < THIRSTY_AT:
        out.append({"kind": "thirsty", "text": "你有点渴——猫房的饮水机一直有水"})
    if not state.get("asleep") and state["energy"] < SLEEPY_AT:
        out.append({"kind": "sleepy", "text": "你困得眼皮打架——找个舒服地方睡"})
    if float(state.get("poop_pressure", 0)) >= POOP_WANT_AT:
        if int(state.get("litter", 1)) >= 3:
            out.append({"kind": "poop_blocked",
                        "text": "你想拉屎，但猫砂盆满了你嫌脏不想用——憋着难受，"
                                "去找个人挠挠让他来铲"})
        else:
            out.append({"kind": "poop", "text": "你想拉屎了——去猫房的猫砂盆解决"})
    return out


# ---------- PetLog（append-only jsonl）----------
def append_petlog(pid: str, actor: str, kind: str, text: str) -> dict:
    """actor: 实体 id（user/角色/宠物自己）| "system"。记不上也不让交互失败。"""
    rec = {"ts": _now(), "actor": actor, "kind": kind, "text": text}
    try:
        p = _dir(pid) / "petlog.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return rec


def read_petlog(pid: str, limit: Optional[int] = None) -> list[dict]:
    p = _dir(pid) / "petlog.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text("utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out[-limit:] if limit else out
