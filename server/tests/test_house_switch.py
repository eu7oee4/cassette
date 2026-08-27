"""小屋总开关（PLAN_house_switch H0）单测。

基座沿用 tests.test_cohabit_queue.QueueBase（world/队列/开关/概率全打桩），再加一只
临时注册的猫（口径抄 tests.test_pet.PetBase：宠物状态根目录指临时区）——总开关的
关键账目全在猫身上（惰性衰减的冻结/结账/对表）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_house_switch -v
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cohabit_queue as cq
import config
import offers
import pet_queue as pq
import pet_store
import state_store
import world
from tests.test_cohabit_queue import QueueBase


class HouseSwitchBase(QueueBase):
    def setUp(self):
        super().setUp()
        self._psr_orig = pet_store.PETS_STATE_ROOT
        pet_store.PETS_STATE_ROOT = self.tmp / "petstate"
        self.pid = self.register_pet()
        pet_store.settle(self.pid)   # 建初始 state（默认四值，updated_at=now）
        offers._offer = None

    def tearDown(self):
        offers._offer = None
        pet_store.PETS_STATE_ROOT = self._psr_orig
        super().tearDown()


class TestSwitchSemantics(HouseSwitchBase):
    def test_active_frozen_split(self):
        """house_active=装了且开着；house_frozen=装了但关着（没装不算冻结）。"""
        self.assertTrue(world.house_active())
        self.assertFalse(world.house_frozen())
        world.set_house_enabled(False)
        self.assertFalse(world.house_active())
        self.assertTrue(world.house_frozen())
        config.COHABIT_ENABLED = False   # 没装的部署：不存在冻结，别停人家的猫钟
        self.assertFalse(world.house_frozen())
        self.assertFalse(world.house_active())

    def test_off_clears_everything_silently(self):
        """关：清 pending（两个队列）、撤 NEXT、邀约静默作废（不走默认答应、
        不发补醒）、落痕事件不唤醒任何人。"""
        cq.enqueue(self.cid, {"kind": "event", "text": "x"})
        with pq._lock:
            pq._pending[self.pid] = [{"ev": None, "text": "y"}]
        with state_store.SCHEDULE_LOCK:
            sched = state_store.read_schedule(self.cid)
            sched["next_wake_at"] = time.time() + 3600
            state_store.write_schedule(sched, self.cid)
        # 已超时的邀约 + 默认答应开着：作废也不该抱走人
        offers._offer = {"id": "t1", "actor": self.cid, "to": "living_room",
                        "room": self.home, "move_motion": "",
                        "deadline": int(time.time()) - 1}
        calls = []
        orig = offers._enqueue
        offers._enqueue = lambda cid, reason: calls.append((cid, reason))
        try:
            cq.switch_house(False)
        finally:
            offers._enqueue = orig
        self.assertFalse(world.house_enabled())
        self.assertEqual(cq._pending, {})
        self.assertEqual(pq._pending, {})
        self.assertIsNone(offers._offer)
        self.assertEqual(calls, [])
        self.assertNotIn("next_wake_at", state_store.read_schedule(self.cid))
        # 落痕在有角色的房间，且（旗已拨下）没唤醒任何人
        evs = world.read_events(self.home, limit=5)
        self.assertTrue(any(e.get("kind") == "house_switch" for e in evs))
        self.assertEqual(cq._pending, {})

    def test_off_rejects_all_entrances(self):
        cq.switch_house(False)
        self.assertFalse(cq.enqueue(self.cid, {"kind": "event", "text": "x"}))
        self.assertFalse(cq.enqueue(self.cid, {"kind": "solo", "text": "x"},
                                    system=False))
        self.assertIsNone(cq.chat_move(self.cid, "living_room"))
        self.assertEqual(cq.chat_move_hint(self.cid), "")
        # 事件照常落盘（数据层不拦，API 层 409），但钩子歇业：没人醒、猫也不醒
        world.act("user", "mm_room", speech="有人吗")
        self.assertEqual(cq._pending, {})
        self.assertEqual(pq._pending, {})

    def test_off_is_idempotent(self):
        cq.switch_house(False)
        n1 = len(world.read_events(self.home, limit=50))
        cq.switch_house(False)   # 已经关着：幂等，不重复落痕
        self.assertEqual(len(world.read_events(self.home, limit=50)), n1)

    def test_on_announce_wakes_present(self):
        """开：落痕事件把各自房间里的角色叫起来（重开的第一口气）。"""
        cq.switch_house(False)
        self._reset_queue()   # 清干净，只看「开」那一下的效果
        cq.switch_house(True)
        self.assertTrue(world.house_enabled())
        self.assertIn(self.cid, cq._pending)
        self.assertTrue(any("休眠中醒了过来" in r["text"]
                            for r in cq._pending[self.cid]))


class TestPetFreeze(HouseSwitchBase):
    def test_clock_stops_and_no_backfill(self):
        """关三天再开：冻结期间查看四值不掉；解冻对表后也不一次性补扣。"""
        before = pet_store.read_state(self.pid)
        cq.switch_house(False)
        # 模拟已经关了三天：baseline 时间直接拨回 72h 前（settle 之后的直写）
        s = pet_store._load_raw(self.pid)
        s["updated_at"] = time.time() - 72 * 3600
        pet_store._write_json(pet_store._dir(self.pid) / "state.json", s)
        # 冻结期间查看：钟停在 baseline 时刻，四值不掉、憋屎不涨
        mid = pet_store.read_state(self.pid)
        self.assertEqual(mid["satiety"], before["satiety"])
        self.assertEqual(mid["energy"], before["energy"])
        self.assertEqual(round(float(mid["poop_pressure"])),
                         round(float(before["poop_pressure"])))
        self.assertEqual(pet_store.needs(mid), [])   # 不会「饿死态+憋屎爆表」
        # 解冻：对表（updated_at=now），冻结那段对猫不存在
        cq.switch_house(True)
        after = pet_store.read_state(self.pid)
        self.assertEqual(after["satiety"], before["satiety"])
        self.assertAlmostEqual(float(pet_store._load_raw(self.pid)["updated_at"]),
                               time.time(), delta=5)

    def test_sleeping_pet_stays_asleep(self):
        """睡着的猫冻结中不会因为精力「补涨」而睡醒。"""
        pet_store.apply_interaction(self.pid, new_stats={"energy": 50}, asleep=True)
        cq.switch_house(False)
        s = pet_store._load_raw(self.pid)
        s["updated_at"] = time.time() - 10 * 3600   # 睡 10h 本该回满自醒
        pet_store._write_json(pet_store._dir(self.pid) / "state.json", s)
        mid = pet_store.read_state(self.pid)
        self.assertTrue(mid["asleep"])
        self.assertEqual(mid["energy"], 50)

    def test_mutate_during_freeze_lands_at_pause_moment(self):
        """冻结中万一有改动漏进来（防御性）：记在关门那一刻，不产生时间流逝。"""
        cq.switch_house(False)
        t0 = float(pet_store._load_raw(self.pid)["updated_at"])
        pet_store.scoop(self.pid)
        self.assertEqual(float(pet_store._load_raw(self.pid)["updated_at"]), t0)


if __name__ == "__main__":
    unittest.main()
