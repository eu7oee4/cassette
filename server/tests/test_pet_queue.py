"""pet_queue.py（猫的触发与闸 P2）单测。

基座沿用 tests.test_pet.PetBase（世界+宠物状态全在临时目录、DeepSeek 罐头替身），
再加：开关强制打开、pet_engine.wake 打桩（录调用不真起）、概率必中、队列内存态清零。
worker 线程不起——_drain/_tick 直接同步调（口径同 test_cohabit_queue）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_pet_queue -v
"""
import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import pet_engine
import pet_queue as pq
import world
from tests.test_pet import PetBase


def reset_pet_queue():
    with pq._lock:
        pq._pending.clear()
        pq._chain.clear()
        pq._last_wake.clear()
        pq._need_last.clear()
        pq._cooldown.clear()
    pq._signal.clear()
    pq._initiator["id"] = None


class PetQueueBase(PetBase):
    def setUp(self):
        super().setUp()
        self._enabled_orig = config.COHABIT_ENABLED
        config.COHABIT_ENABLED = True
        self._wake_orig = pet_engine.wake
        self.wakes: list[tuple] = []
        pet_engine.wake = lambda pid, reasons: self.wakes.append((pid, reasons))
        self._random_orig = pq.random
        pq.random = types.SimpleNamespace(random=lambda: 0.0)   # 概率必中
        reset_pet_queue()

    def tearDown(self):
        reset_pet_queue()
        config.COHABIT_ENABLED = self._enabled_orig
        pet_engine.wake = self._wake_orig
        pq.random = self._random_orig
        super().tearDown()


class TestEventWake(PetQueueBase):
    def ev(self, actor="user", text="小猫咪", etype="speech"):
        return {"actor": actor, "type": etype, "text": text}

    def test_event_wakes_pet_through_queue(self):
        world.move("user", self.cat_room)
        pq.on_room_event(self.cat_room, self.ev())
        self.assertIn(self.pid, pq._pending)
        pq._drain()
        pid, reasons = self.wakes[-1]
        self.assertEqual(pid, self.pid)
        self.assertIn("说话", reasons[0])
        self.assertIn("听不懂内容", reasons[0])     # 只听语气和熟词的口径进醒因
        self.assertEqual(pq._chain[self.pid], 1)    # 事件醒来计连发

    def test_author_and_other_room_excluded(self):
        pq.on_room_event(self.cat_room, self.ev(actor=self.pid, etype="action"))
        self.assertNotIn(self.pid, pq._pending)     # 自己的动静不吵自己
        pq.on_room_event("living_room", self.ev())  # 猫不在客厅
        self.assertNotIn(self.pid, pq._pending)

    def test_gap_and_chain_and_reset(self):
        pq._last_wake[self.pid] = time.time()
        pq.on_room_event(self.cat_room, self.ev())
        self.assertNotIn(self.pid, pq._pending)     # 最小间隔拦住
        pq._last_wake.clear()
        pq._chain[self.pid] = pq.PET_CHAIN_N
        pq.on_room_event(self.cat_room, self.ev())
        self.assertNotIn(self.pid, pq._pending)     # 连发上限拦住
        pq.external_for_pet(self.pid)               # 被撸了 = 外部输入
        pq.on_room_event(self.cat_room, self.ev())
        self.assertIn(self.pid, pq._pending)

    def test_forget_events_drops_queued_reason(self):
        """猫这边同理：删了事件，还没执行的醒因跟着撤。"""
        world.move("user", self.cat_room)
        ev = {"id": "e1", "actor": "user", "type": "speech", "text": "小猫咪"}
        pq.on_room_event(self.cat_room, ev)
        self.assertIn(self.pid, pq._pending)
        self.assertEqual(pq.forget_events({"e1"}), 1)
        self.assertNotIn(self.pid, pq._pending)
        pq._drain()
        self.assertEqual(self.wakes, [])          # 撤干净了，不会再醒

    def test_drain_passes_texts_to_engine(self):
        world.move("user", self.cat_room)
        pq.on_room_event(self.cat_room, {"id": "e2", "actor": "user",
                                         "type": "speech", "text": "小猫咪"})
        pq._drain()
        _, reasons = self.wakes[-1]
        self.assertTrue(all(isinstance(r, str) for r in reasons))   # 引擎收的仍是文本

    def test_probability_gate(self):
        pq.random = types.SimpleNamespace(random=lambda: 0.9)   # > PET_EVENT_PROB
        pq.on_room_event(self.cat_room, self.ev())
        self.assertNotIn(self.pid, pq._pending)


class TestTick(PetQueueBase):
    def test_need_wake_is_autonomous_and_cooled(self):
        self.force_state(satiety=30.0)              # 饿
        pq._chain[self.pid] = 2
        pq._tick(time.time())
        pid, reasons = self.wakes[-1]
        self.assertIn("饿", reasons[0])
        self.assertNotIn(self.pid, pq._chain)       # 自主醒清连发（同 solo 口径）
        n = len(self.wakes)
        pq._last_wake.clear()                       # 排除最小间隔干扰，单测需求冷却
        pq._tick(time.time())
        self.assertEqual(len(self.wakes), n)        # 同类需求 30 分钟内不重复催

    def test_enforce_runs_before_model(self):
        self.force_state(satiety=5.0)               # 硬地板区间
        world.move(self.pid, "living_room")
        pq._tick(time.time())
        self.assertEqual(self.wakes, [])            # 干了正事，不叠模型醒
        self.assertEqual(world.location_of(self.pid), self.cat_room)   # 真走过去吃了

    def test_asleep_skips(self):
        self.force_state(asleep=True, energy=50.0, satiety=30.0)
        pq._tick(time.time())
        self.assertEqual(self.wakes, [])

    def test_idle_wake_when_content(self):
        pq._tick(time.time())                       # 状态默认良好 + 概率必中 → 溜达
        self.assertTrue(self.wakes)
        self.assertIn("溜达", self.wakes[-1][1][0])

    def test_error_cooldown(self):
        def boom(pid, reasons):
            raise RuntimeError("模型挂了")
        pet_engine.wake = boom
        world.move("user", self.cat_room)
        pq.on_room_event(self.cat_room, {"actor": "user", "type": "speech", "text": "x"})
        pq._drain()
        self.assertGreater(pq._cooldown.get(self.pid, 0), time.time())
        pq.on_room_event(self.cat_room, {"actor": "user", "type": "speech", "text": "y"})
        self.assertNotIn(self.pid, pq._pending)     # 冷却期不再入队


class TestPaused(PetQueueBase):
    """机主的暂停键管到猫身上（2026-08-19）：暂停只停执行，事件照常攒。"""

    def setUp(self):
        super().setUp()
        import cohabit_queue
        self.cq = cohabit_queue
        self._paused_orig = cohabit_queue.paused()
        cohabit_queue.set_paused(True)

    def tearDown(self):
        self.cq.set_paused(self._paused_orig)
        super().tearDown()

    def test_events_still_queue_but_drain_holds(self):
        world.move("user", self.cat_room)
        pq.on_room_event(self.cat_room, {"actor": "user", "type": "speech", "text": "小猫咪"})
        self.assertIn(self.pid, pq._pending)      # 事件照常落，pending 照常合并
        pq._drain()
        self.assertEqual(self.wakes, [])          # 但不执行
        self.cq.set_paused(False)
        pq._drain()
        self.assertTrue(self.wakes)               # 按开始一口气恢复

    def test_tick_and_enforce_hold(self):
        self.force_state(satiety=5.0)             # 又饿到硬地板又该溜达
        world.move(self.pid, "living_room")
        pq._tick(time.time())
        self.assertEqual(self.wakes, [])
        self.assertEqual(world.location_of(self.pid), "living_room")   # 硬地板也停着
        self.cq.set_paused(False)
        pq._tick(time.time())
        self.assertEqual(world.location_of(self.pid), self.cat_room)   # 恢复后当轮补上


if __name__ == "__main__":
    unittest.main()
