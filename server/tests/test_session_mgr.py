"""session_mgr（PLAN_sdk S1/PR4）单测：注册/独占组/收摊/看守三行为，全部离线。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_session_mgr -v
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import session_mgr as sm


class MgrTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sm.set_event_loop(asyncio.get_running_loop())
        sm._registry.clear()

    async def _idle_runner(self, handle):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    async def test_register_and_exclusive_group(self):
        h1 = sm.start("cass", "game", self._idle_runner, exclusive_group="computer")
        await asyncio.sleep(0.01)
        self.assertTrue(h1.alive())
        self.assertIs(sm.get("cass", "game"), h1)
        self.assertIs(sm.group_alive("computer"), h1)
        # 同键拒绝
        r = sm.start("cass", "game", self._idle_runner)
        self.assertIsInstance(r, dict)
        self.assertFalse(r["ok"])
        # 同独占组（别的角色/场景）拒绝，且是拒绝不是杀
        r2 = sm.start("default", "code", self._idle_runner, exclusive_group="computer")
        self.assertFalse(r2["ok"])
        self.assertIn("独占组", r2["error"])
        self.assertTrue(h1.alive())
        # 不同组不受影响
        h3 = sm.start("cass", "chat", self._idle_runner)
        await asyncio.sleep(0.01)
        self.assertTrue(h3.alive())

    async def test_stop_and_unregister(self):
        h = sm.start("cass", "game", self._idle_runner)
        await asyncio.sleep(0.01)
        self.assertTrue(sm.stop("cass", "game", "test"))
        await asyncio.sleep(0.05)
        self.assertFalse(h.alive())
        self.assertIsNone(sm.get("cass", "game"))
        self.assertEqual(h.stop_reason, "test")
        self.assertFalse(sm.stop("cass", "game"))      # 已经没了

    async def test_runner_exit_unregisters(self):
        async def quick(handle):
            await asyncio.sleep(0.01)
        sm.start("cass", "game", quick)
        await asyncio.sleep(0.05)
        self.assertIsNone(sm.get("cass", "game"))

    async def test_queue_threadsafe_put(self):
        h = sm.start("cass", "game", self._idle_runner)
        await asyncio.sleep(0.01)
        await asyncio.to_thread(h.put_threadsafe, "hello")   # 从工作线程塞
        got = await asyncio.wait_for(h.queue.get(), 1)
        self.assertEqual(got, "hello")

    async def test_watchdog_idle_stop(self):
        stopped = []
        w = sm.WatchPolicy(idle_stop_sec=0.05, tick_sec=0.03,
                           on_idle_stop=lambda h: stopped.append(h.char_id))
        h = sm.start("cass", "game", self._idle_runner, watch=w)
        await asyncio.sleep(0.3)
        self.assertEqual(stopped, ["cass"])
        self.assertFalse(h.alive())
        self.assertEqual(h.stop_reason, "idle")

    async def test_watchdog_touch_defers_idle(self):
        stopped = []
        w = sm.WatchPolicy(idle_stop_sec=0.2, tick_sec=0.03,
                           on_idle_stop=lambda h: stopped.append(1))
        h = sm.start("cass", "game", self._idle_runner, watch=w)
        for _ in range(6):                 # 持续有动静：不该被收
            await asyncio.sleep(0.05)
            h.touch()
        self.assertEqual(stopped, [])
        self.assertTrue(h.alive())

    async def test_watchdog_wait_nudge_once(self):
        nudges = []
        w = sm.WatchPolicy(idle_stop_sec=999, wait_nudge_sec=0.05, tick_sec=0.03,
                           on_wait_nudge=lambda h: nudges.append(1))
        h = sm.start("cass", "game", self._idle_runner, watch=w)
        await asyncio.sleep(0.01)
        h.wait_user()
        await asyncio.sleep(0.25)
        self.assertEqual(nudges, [1])      # 只提醒一次
        h.touch()                          # TA 回话了
        h.wait_user()                      # 又停下来等
        await asyncio.sleep(0.15)
        self.assertEqual(nudges, [1, 1])   # 新一轮等待可以再提醒

    async def test_watchdog_reopening_marker_holds(self):
        stopped = []
        w = sm.WatchPolicy(idle_stop_sec=0.05, tick_sec=0.03,
                           on_idle_stop=lambda h: stopped.append(1))
        h = sm.start("cass", "game", self._idle_runner, watch=w)
        await asyncio.sleep(0.01)
        h.reopening = True                 # 滚动重开进行中：看守必须停手
        await asyncio.sleep(0.2)
        self.assertEqual(stopped, [])
        self.assertTrue(h.alive())
        h.reopening = False
        h.last_activity = time.time() - 1  # 重开完且真 idle 了才收
        await asyncio.sleep(0.1)
        self.assertEqual(stopped, [1])


if __name__ == "__main__":
    unittest.main()
