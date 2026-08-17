"""宠物注册表（PLAN_pet P0）：pet 是第三类实体，不是 character。

一只宠物 = server/pets/<pet_id>/ 一个目录（整个目录 gitignore，与 characters 同构
——通用代码入库、实例是私人数据）：
    pet.json     接线：{"display_name", "engine": {...}}（engine 段 P1 猫引擎才消费）
    persona.md   猫人设（P1 猫引擎的 system prompt）

pet 在世界里有位置、可被抱（carry）、可当事件 actor、出现在「这里有谁」；
characters 的全部管线（手机线程 / persona 引擎 / wake 预算 / 通讯录）都不认识它，
醒来队列也不认（猫怎么醒是 P1 猫引擎自己的触发线）。本模块只管「它存在、它叫什么」。

结构性口径（在别处执行，记在这里备查，全文见 PLAN_pet）：
- 猫洞：world.can_enter 对 pet 豁免——锁挡人不挡猫，信息安全靠注入不靠门；
- 无经历流：world._append_experience 跳过 pet——「猫的上下文只含当前房间」
  是信息通道的结构性封堵，不是省 IO。
"""
import json

from typing import Optional

import config

PETS_DIR = config.BASE_DIR / "pets"


def ids() -> list[str]:
    if not PETS_DIR.is_dir():
        return []
    return [d.name for d in sorted(PETS_DIR.iterdir())
            if d.is_dir() and (d / "pet.json").exists()]


def _conf(pid: str) -> dict:
    try:
        return json.loads((PETS_DIR / pid / "pet.json").read_text("utf-8"))
    except Exception:
        return {}


def display_name(pid: str) -> str:
    return _conf(pid).get("display_name") or pid


def is_pet(entity_id) -> bool:
    return entity_id in ids()


def match(name_or_id: str) -> Optional[str]:
    """按 id 或显示名认宠物（CARRY 解析、用户 move 的 carry 参数用）；认不出 → None。"""
    for pid in ids():
        if name_or_id in (pid, display_name(pid)):
            return pid
    return None
