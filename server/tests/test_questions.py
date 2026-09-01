"""questions（PLAN_chatui §3.5/U4）：AskUserQuestion 的问答卡。主线——挂起/
作答/不答/超时/后端重启作废；外加回调面（updated_input 回填答案）和 sse
question 事件（assistant 事件里的 tool_use 一出现就推卡）。全部离线。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_questions -v
"""
import asyncio
import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify
import questions

QS = [{"question": "晚饭吃什么？", "header": "晚饭",
       "options": [{"label": "面条", "description": "热汤面"},
                   {"label": "米饭", "description": "配菜另点"}],
       "multiSelect": False}]


class QuestionBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.barks: list[str] = []
        self._bark_orig = notify.bark_push
        notify.bark_push = lambda text, title=None: self.barks.append(text) or True
        questions._pending.clear()

    def tearDown(self):
        notify.bark_push = self._bark_orig
        questions._pending.clear()


class AskDecideTest(QuestionBase):
    """挂起 → 卡在册 + Bark；作答/不答原地返回；超时自动收；卡答完即清。"""

    async def test_hang_registers_card_then_answer(self):
        task = asyncio.ensure_future(questions.ask(
            "cass", questions=QS, question_id="toolu_q1"))
        await asyncio.sleep(0)
        cards = questions.pending()
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual((c["id"], c["char"]), ("toolu_q1", "cass"))
        self.assertEqual(c["questions"][0]["question"], "晚饭吃什么？")
        self.assertNotIn("future", c)                  # REST 面不带进程内脏器
        self.assertTrue(questions.waiting("cass"))
        self.assertFalse(questions.waiting("default"))
        self.assertEqual(len(self.barks), 1)
        self.assertIn("晚饭吃什么", self.barks[0])
        r = questions.decide("toolu_q1", {"晚饭吃什么？": "面条"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["state"], "answered")
        self.assertEqual(await task, ({"晚饭吃什么？": "面条"}, ""))
        self.assertEqual(questions.pending(), [])      # 卡答完即清
        self.assertFalse(questions.waiting("cass"))

    async def test_custom_answer_is_any_string(self):
        """自定义答案（§3.5 场景 2）：answers 的值不限于 options 里的 label。"""
        task = asyncio.ensure_future(questions.ask(
            "cass", questions=QS, question_id="q2"))
        await asyncio.sleep(0)
        questions.decide("q2", {"晚饭吃什么？": "都不想吃，点个披萨"})
        self.assertEqual((await task)[0]["晚饭吃什么？"], "都不想吃，点个披萨")

    async def test_dismiss_carries_note(self):
        task = asyncio.ensure_future(questions.ask(
            "cass", questions=QS, question_id="q3"))
        await asyncio.sleep(0)
        r = questions.decide("q3", None, "现在没空，回头说")
        self.assertEqual(r["state"], "dismissed")
        self.assertEqual(await task, (None, "现在没空，回头说"))

    async def test_timeout_cleans(self):
        got = await questions.ask("cass", questions=QS, timeout=0.01)
        self.assertIsNone(got)
        self.assertEqual(questions.pending(), [])
        self.assertFalse(questions.waiting("cass"))

    async def test_decide_unknown_id_loud(self):
        r = questions.decide("nope", {"q": "a"})
        self.assertFalse(r["ok"])
        self.assertIn("问答卡", r["error"])

    async def test_restart_voids_pending(self):
        """内存态是特性：后端重启即作废，他想问会再问。"""
        task = asyncio.ensure_future(questions.ask(
            "cass", questions=QS, question_id="qr"))
        await asyncio.sleep(0)
        fresh = importlib.reload(questions)            # 重启＝内存清零
        r = fresh.decide("qr", {"q": "a"})
        self.assertFalse(r["ok"])
        self.assertEqual(fresh.pending(), [])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class QuestionCallbackTest(QuestionBase):
    """chat_loop._permit_gate 的 AskUserQuestion 分支：答→Allow 且
    updated_input=原 input+answers（回填机制 09-01 探针实证）；
    不答→Deny 带机主的话；超时→Deny 有交代。"""

    def _cb(self, cid="cass"):
        import chat_loop
        import session_mgr as sm
        handle = sm.LoopHandle(char_id=cid, scene="chat")
        return chat_loop._permit_gate(handle)

    async def test_answer_backfills_updated_input(self):
        from claude_agent_sdk import PermissionResultAllow
        cb = self._cb()
        ctx = SimpleNamespace(title="", tool_use_id="toolu_qa")
        task = asyncio.ensure_future(
            cb("AskUserQuestion", {"questions": QS}, ctx))
        await asyncio.sleep(0)
        card = questions.pending()[0]
        self.assertEqual(card["id"], "toolu_qa")       # 单号=那次调用本身
        questions.decide("toolu_qa", {"晚饭吃什么？": "米饭"})
        out = await task
        self.assertIsInstance(out, PermissionResultAllow)
        self.assertEqual(out.updated_input["questions"], QS)   # 原 input 原样保留
        self.assertEqual(out.updated_input["answers"], {"晚饭吃什么？": "米饭"})

    async def test_dismiss_denies_with_note(self):
        from claude_agent_sdk import PermissionResultDeny
        cb = self._cb()
        task = asyncio.ensure_future(cb(
            "AskUserQuestion", {"questions": QS},
            SimpleNamespace(title="", tool_use_id="qd")))
        await asyncio.sleep(0)
        questions.decide("qd", None, "在开会")
        out = await task
        self.assertIsInstance(out, PermissionResultDeny)
        self.assertIn("在开会", out.message)
        self.assertFalse(out.interrupt)

    async def test_timeout_denies_with_message(self):
        from claude_agent_sdk import PermissionResultDeny
        orig = questions.TIMEOUT_SEC
        questions.TIMEOUT_SEC = 0.01
        try:
            cb = self._cb()
            out = await cb("AskUserQuestion", {"questions": QS},
                           SimpleNamespace(title="", tool_use_id="qt"))
            self.assertIsInstance(out, PermissionResultDeny)
            self.assertTrue(out.message)               # 有交代，不是空拒
        finally:
            questions.TIMEOUT_SEC = orig

    async def test_write_tools_still_route_to_permits(self):
        """问答分支别抢写类的路：Edit 照旧走 permits。"""
        import permits
        permits._pending.clear()
        cb = self._cb()
        task = asyncio.ensure_future(cb(
            "Edit", {"file_path": "x.py"},
            SimpleNamespace(title="", tool_use_id="tw")))
        await asyncio.sleep(0)
        self.assertEqual(permits.pending()[0]["id"], "tw")
        self.assertEqual(questions.pending(), [])
        permits.decide("tw", False)
        await task
        permits._pending.clear()


class SseQuestionEventTest(unittest.TestCase):
    """sse._question_events：assistant 事件里的 AskUserQuestion tool_use →
    一条 question SSE（单号=tool_use_id）；别的工具/文本块不触发。"""

    def test_emits_card_for_ask_user_question(self):
        import json
        import sse
        ev = {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "我问问你"},
            {"type": "tool_use", "id": "toolu_s1", "name": "AskUserQuestion",
             "input": {"questions": QS}},
        ]}}
        chunks = list(sse._question_events(ev))
        self.assertEqual(len(chunks), 1)
        payload = json.loads(chunks[0].decode("utf-8")[len("data: "):])
        self.assertEqual(payload["type"], "question")
        self.assertEqual(payload["id"], "toolu_s1")
        self.assertEqual(payload["questions"], QS)
        self.assertGreater(payload["deadline"], 0)

    def test_other_tools_do_not_emit(self):
        import sse
        ev = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t", "name": "Read",
             "input": {"file_path": "a"}},
        ]}}
        self.assertEqual(list(sse._question_events(ev)), [])


if __name__ == "__main__":
    unittest.main()
