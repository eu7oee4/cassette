"""卡的收场态账 + 第二种让路（10-30 机主拍板，随 PLAN_toolmount §9 第 7 步落地）：
permits / questions 的 closed()（timeout / yielded_chat / yielded_wake / denied / allowed）；
wake_sdk.enqueue_wake 入队时让路（yielded_wake）；两种让路给模型的原话不同。全部离线。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_cards_closed -v
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import permits
import questions


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import notify
        permits._pending.clear(); permits._closed.clear()
        questions._pending.clear(); questions._closed.clear()
        self._bark = notify.bark_push
        notify.bark_push = lambda text, title=None: True

    def tearDown(self):
        import notify
        notify.bark_push = self._bark
        permits._pending.clear(); permits._closed.clear()
        questions._pending.clear(); questions._closed.clear()


class ClosedLedgerTest(_Base):
    async def test_permit_states(self):
        import chat_loop
        # 超时
        self.assertIsNone(await permits.ask("cass", tool="Bash", summary="ls", timeout=0.01, permit_id="t1"))
        # 批 / 拒
        for pid, allow in (("t2", True), ("t3", False)):
            task = asyncio.ensure_future(permits.ask("cass", tool="Bash", summary="ls", timeout=5, permit_id=pid))
            await asyncio.sleep(0.01)
            permits.decide(pid, allow)
            await task
        # 两种让路
        for pid, how in (("t4", "chat"), ("t5", "wake")):
            task = asyncio.ensure_future(permits.ask("cass", tool="Bash", summary="ls", timeout=5,
                                                     permit_id=pid, wake=True))
            await asyncio.sleep(0.01)
            chat_loop._yield_wake_cards("cass", how)
            res = await asyncio.wait_for(task, 1)
            self.assertEqual(res, (False, chat_loop.YIELD_MSG if how == "chat" else chat_loop.YIELD_MSG_WAKE))
        states = {c["id"]: c["state"] for c in permits.closed("cass")}
        self.assertEqual(states, {"t1": "timeout", "t2": "allowed", "t3": "denied",
                                  "t4": "yielded_chat", "t5": "yielded_wake"})
        self.assertEqual(permits.closed("default"), [])
        self.assertEqual(permits.pending(), [])

    async def test_question_states(self):
        import chat_loop
        self.assertIsNone(await questions.ask("cass", questions=[{"question": "q"}], timeout=0.01, question_id="q1"))
        task = asyncio.ensure_future(questions.ask("cass", questions=[{"question": "q"}], timeout=5,
                                                   question_id="q2", wake=True))
        await asyncio.sleep(0.01)
        chat_loop._yield_wake_cards("cass", "wake")
        self.assertEqual(await asyncio.wait_for(task, 1), (None, chat_loop.YIELD_MSG_WAKE))
        task = asyncio.ensure_future(questions.ask("cass", questions=[{"question": "q"}], timeout=5, question_id="q3"))
        await asyncio.sleep(0.01)
        questions.decide("q3", {"q": "a"})
        await task
        states = {c["id"]: c["state"] for c in questions.closed("cass")}
        self.assertEqual(states, {"q1": "timeout", "q2": "yielded_wake", "q3": "answered"})

    async def test_closed_pruned_after_keep(self):
        await permits.ask("cass", tool="Bash", summary="ls", timeout=0.01, permit_id="old")
        permits._closed["old"]["ts"] -= permits.CLOSED_KEEP_SEC + 1
        self.assertEqual(permits.closed("cass"), [])


class EnqueueWakeYieldsTest(_Base):
    async def test_enqueue_wake_yields_wake_cards(self):
        import chat_loop
        import session_mgr as sm
        import wake_sdk
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        orig_ensure, orig_yield = chat_loop._ensure_loop, chat_loop._yield_wake_cards
        seen = []
        chat_loop._ensure_loop = lambda cid: handle
        chat_loop._yield_wake_cards = lambda cid, how="chat": seen.append((cid, how))
        try:
            self.assertTrue(wake_sdk.enqueue_wake("cass", "delegate"))
            self.assertEqual(seen, [("cass", "wake")])
            self.assertEqual(handle.queue.qsize(), 1)
            turn = handle.queue.get_nowait()
            self.assertEqual(turn.wake_trigger, "delegate")
            # 同一醒因在队里不重复入队，也不再让路
            handle.queue.put_nowait(turn)
            self.assertTrue(wake_sdk.enqueue_wake("cass", "delegate"))
            self.assertEqual(seen, [("cass", "wake")])
        finally:
            chat_loop._ensure_loop, chat_loop._yield_wake_cards = orig_ensure, orig_yield

    def test_delegate_headline(self):
        import wake_sdk
        self.assertIn("派出去的活", wake_sdk.wake_headline("delegate"))


if __name__ == "__main__":
    unittest.main()
