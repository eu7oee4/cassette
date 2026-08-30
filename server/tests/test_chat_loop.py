"""chat_loop（PLAN_sdk S2/PR10）单测：判脏矩阵、铸造/重铸触发、轮结局、收摊纪律。
引擎用假 client（脚本化消息流），forge 用桩（不碰 ~/.claude）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_chat_loop -v
"""
import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat_loop
import session_mgr as sm
from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import StreamEvent


def _m(role, text, ts=1000):
    return {"role": role, "text": text, "ts": ts}


def _led(*pairs):
    return [{"r": r, "h": chat_loop._h(t), "ts": 1000} for r, t in pairs]


class DivergenceTest(unittest.TestCase):
    """判脏矩阵（设计稿一）：干净/窗口滑动/编辑/删除/stale 容忍/口径。"""

    HIST4 = [_m("user", "a"), _m("assistant", "b"), _m("user", "c"),
             _m("assistant", "d")]

    def test_identical_clean(self):
        led = _led(("user", "a"), ("assistant", "b"), ("user", "c"),
                   ("assistant", "d"))
        self.assertIsNone(chat_loop.divergence(led, self.HIST4))

    def test_window_slide_clean(self):
        """账比窗长（窗口滑动）：窗=账尾 → 干净。"""
        led = _led(("user", "x"), ("assistant", "y"),
                   ("user", "a"), ("assistant", "b"), ("user", "c"),
                   ("assistant", "d"))
        self.assertIsNone(chat_loop.divergence(led, self.HIST4))

    def test_ts_change_not_dirty(self):
        """假脏口径：ts 不参与 hash——只改时间戳不算编辑。"""
        led = _led(("user", "a"), ("assistant", "b"))
        hist = [_m("user", "a", ts=1), _m("assistant", "b", ts=999999)]
        self.assertIsNone(chat_loop.divergence(led, hist))

    def test_edit_middle_dirty(self):
        led = _led(("user", "a"), ("assistant", "b"), ("user", "c"),
                   ("assistant", "d"))
        hist = [_m("user", "a"), _m("assistant", "改了"), _m("user", "c"),
                _m("assistant", "d")]
        self.assertEqual(chat_loop.divergence(led, hist), "dirty")

    def test_delete_middle_dirty(self):
        led = _led(("user", "a"), ("assistant", "b"), ("user", "c"),
                   ("assistant", "d"))
        hist = [_m("user", "a"), _m("user", "c"), _m("assistant", "d")]
        self.assertEqual(chat_loop.divergence(led, hist), "dirty")

    def test_stale_assistant_tail_tolerated(self):
        """连发竞态/wake 气泡没拉走：账尾多出纯 assistant → stale 不脏。"""
        led = _led(("user", "a"), ("assistant", "b"), ("assistant", "醒来说的"))
        hist = [_m("user", "a"), _m("assistant", "b")]
        self.assertEqual(chat_loop.divergence(led, hist), "stale")

    def test_missing_user_tail_dirty(self):
        """账尾缺的包含 user 条目=真删除，不在容忍范围。"""
        led = _led(("user", "a"), ("assistant", "b"), ("user", "c"))
        hist = [_m("user", "a"), _m("assistant", "b")]
        self.assertEqual(chat_loop.divergence(led, hist), "dirty")

    def test_stale_beyond_tolerance_dirty(self):
        led = _led(("user", "a"), *[("assistant", f"x{i}") for i in range(12)])
        hist = [_m("user", "a")]
        self.assertEqual(chat_loop.divergence(led, hist), "dirty")

    def test_hist_longer_than_ledger_dirty(self):
        """窗口带来了账之前没见过的更老历史（改了 sendHistoryCap 等）→ 重铸收进来。"""
        led = _led(("user", "c"), ("assistant", "d"))
        hist = [_m("user", "a"), _m("assistant", "b"), _m("user", "c"),
                _m("assistant", "d")]
        self.assertEqual(chat_loop.divergence(led, hist), "dirty")

    def test_empty_cases(self):
        self.assertIsNone(chat_loop.divergence([], []))
        self.assertEqual(chat_loop.divergence([], [_m("user", "a")]), "dirty")
        self.assertEqual(chat_loop.divergence(_led(("user", "a")), []), "dirty")


# ---------- 泵 ----------

def _se(event):
    return StreamEvent(uuid="u", session_id="s", event=event)


def _text_events(text):
    return [
        _se({"type": "content_block_start", "content_block": {"type": "text"}}),
        _se({"type": "content_block_delta",
             "delta": {"type": "text_delta", "text": text}}),
    ]


def _result(text, is_error=False, subtype="success"):
    return ResultMessage(subtype=subtype, duration_ms=0, duration_api_ms=0,
                         is_error=is_error, num_turns=1, session_id="s",
                         result=None if is_error else text)


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.queries = []
        self.disconnected = False
        self._q: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        pass

    async def query(self, payload):
        # 泵传的是 async 迭代器（一条 user 消息 dict）：抽干存证
        msgs = []
        async for m in payload:
            msgs.append(m)
        self.queries.append(msgs)

    def feed(self, *msgs):
        for m in msgs:
            self._q.put_nowait(m)

    async def receive_messages(self):
        while True:
            m = await self._q.get()
            if m is None:
                return
            yield m

    async def disconnect(self):
        self.disconnected = True


class ChatLoopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sm.set_event_loop(asyncio.get_running_loop())
        sm._registry.clear()
        self.clients: list[FakeClient] = []
        self.forged: list[list[dict]] = []
        self._forge_orig = chat_loop.forge
        self._usage_orig = chat_loop._note_usage
        self._persist_orig = chat_loop._persist_ledger
        chat_loop.forge = types.SimpleNamespace(
            render=lambda msgs, **kw: (self.forged.append(list(msgs)),
                                       f"sid-{len(self.forged)}")[1])
        chat_loop._note_usage = lambda *a: None
        chat_loop._persist_ledger = lambda *a: None

        def factory(options):
            c = FakeClient(options)
            self.clients.append(c)
            return c

        self.handle = sm.LoopHandle(char_id="cass", scene="chat")
        self.task = asyncio.create_task(chat_loop.run(
            self.handle,
            client_factory=factory,
            options_factory=lambda cid, catalog=None:
                types.SimpleNamespace(cwd="/tmp/nowhere", resume=None)))
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        chat_loop.forge = self._forge_orig
        chat_loop._note_usage = self._usage_orig
        chat_loop._persist_ledger = self._persist_orig
        if not self.task.done():
            self.handle.stop_reason = "test-teardown"
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass

    def _turn(self, history, new_text, ts=2000):
        return chat_loop.Turn(
            rid="r1", history=chat_loop.norm_history(history),
            new_msg=_m("user", new_text, ts),
            injection=f"【现在是……】\n眠眠：{new_text}",
            finalize=lambda reply, stored: {"reply": reply, "stored": stored})

    async def _play(self, turn, *feed):
        """塞一轮 + 喂脚本 + 收完整 SSE 输出。"""
        self.handle.queue.put_nowait(turn)
        for _ in range(20):          # 等泵开好 session、发出 query
            await asyncio.sleep(0)
            if self.clients and self.clients[-1].queries:
                break
        if feed:
            self.clients[-1].feed(*feed)
        chunks = []
        while True:
            c = await asyncio.wait_for(turn.out.get(), timeout=2)
            if c is None:
                return chunks
            chunks.append(c)

    async def test_first_turn_forges_and_replies(self):
        hist = [_m("user", "早"), _m("assistant", "早，小狗")]
        turn = self._turn(hist, "在吗")
        chunks = await self._play(turn, *_text_events("在。"), _result("在。"))
        blob = b"".join(chunks)
        self.assertIn(b'"type": "text"', blob)
        self.assertIn(b'"type": "done"', blob)
        # 进场铸造：铸的是权威窗口（不含新消息），resume 挂上了 sid
        self.assertEqual(len(self.forged), 1)
        self.assertEqual([m["text"] for m in self.forged[0]], ["早", "早，小狗"])
        self.assertEqual(self.clients[0].options.resume, "sid-1")
        # 入账：窗口 2 条 + 本轮一来一回
        led = self.handle.meta["ledger"]
        self.assertEqual([e["r"] for e in led],
                         ["user", "assistant", "user", "assistant"])
        self.assertEqual(led[-1]["h"], chat_loop._h("在。"))

    async def test_clean_append_reuses_session(self):
        hist = [_m("user", "早"), _m("assistant", "早，小狗")]
        await self._play(self._turn(hist, "在吗"),
                         *_text_events("在。"), _result("在。"))
        # 第二轮：权威 = 老窗口 + 上轮一来一回 → 纯追加，不重铸不换 client
        hist2 = hist + [_m("user", "在吗", 2000), _m("assistant", "在。", 2001)]
        await self._play(self._turn(hist2, "陪我"),
                         *_text_events("嗯。"), _result("嗯。"))
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(len(self.forged), 1)

    async def test_edit_triggers_reforge(self):
        hist = [_m("user", "早"), _m("assistant", "早，小狗")]
        await self._play(self._turn(hist, "在吗"),
                         *_text_events("在。"), _result("在。"))
        # TA 编辑了历史里他的话 → 脏 → 关旧开新、按新权威整体重铸
        hist2 = [_m("user", "早"), _m("assistant", "改过的话"),
                 _m("user", "在吗", 2000), _m("assistant", "在。", 2001)]
        await self._play(self._turn(hist2, "陪我"),
                         *_text_events("嗯。"), _result("嗯。"))
        self.assertEqual(len(self.clients), 2)
        self.assertTrue(self.clients[0].disconnected)
        self.assertEqual(len(self.forged), 2)
        self.assertEqual([m["text"] for m in self.forged[1]],
                         ["早", "改过的话", "在吗", "在。"])

    async def test_stale_assistant_tail_no_reforge(self):
        """请求发出时上轮回复还没进 app 历史（连发竞态）→ 不重铸。"""
        hist = [_m("user", "早"), _m("assistant", "早，小狗")]
        await self._play(self._turn(hist, "在吗"),
                         *_text_events("在。"), _result("在。"))
        # 第二轮的窗口缺上轮回复（assistant 尾巴）——stale 容忍
        hist2 = hist + [_m("user", "在吗", 2000)]
        await self._play(self._turn(hist2, "陪我"),
                         *_text_events("嗯。"), _result("嗯。"))
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(len(self.forged), 1)

    async def test_error_result_closes_session(self):
        hist = [_m("user", "早"), _m("assistant", "早，小狗")]
        turn = self._turn(hist, "在吗")
        chunks = await self._play(turn, _result("", is_error=True,
                                                subtype="error_during_execution"))
        blob = b"".join(chunks)
        self.assertIn(b'"type": "error"', blob)
        self.assertIn(b'"type": "done"', blob)
        self.assertTrue(self.clients[0].disconnected)
        # 下一轮：账清了 → 视同脏 → 重新铸+新 client
        await self._play(self._turn(hist, "还在吗"),
                         *_text_events("在。"), _result("在。"))
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(len(self.forged), 2)

    async def test_eof_mid_turn_gets_error_ending(self):
        """流断在半路：这轮以 error+done 收尾（每个 rid 恰好一个结局）。"""
        hist = [_m("user", "早"), _m("assistant", "早，小狗")]
        turn = self._turn(hist, "在吗")
        self.handle.queue.put_nowait(turn)
        for _ in range(20):
            await asyncio.sleep(0)
            if self.clients and self.clients[-1].queries:
                break
        self.clients[-1].feed(*_text_events("说到一半"))
        self.clients[-1]._q.put_nowait(None)     # EOF
        chunks = []
        while True:
            c = await asyncio.wait_for(turn.out.get(), timeout=2)
            if c is None:
                break
            chunks.append(c)
        blob = b"".join(chunks)
        self.assertIn(b'"type": "error"', blob)
        self.assertIn(b'"type": "done"', blob)
        self.assertEqual(self.handle.meta["ledger"], [])   # 没收尾不入账

    async def test_foreign_cancel_marks_engine_error(self):
        """无 stop_reason 的取消=引擎死外泄（08-30 事故的同类回归）。"""
        class CancelLeakClient(FakeClient):
            async def receive_messages(self):
                raise asyncio.CancelledError()
                yield  # pragma: no cover

        sm._registry.clear()
        handle = sm.LoopHandle(char_id="cass", scene="chat2")
        leak_clients = []

        def factory(options):
            c = CancelLeakClient(options)
            leak_clients.append(c)
            return c

        task = asyncio.create_task(chat_loop.run(
            handle, client_factory=factory,
            options_factory=lambda cid, catalog=None:
                types.SimpleNamespace(cwd="/tmp/nowhere", resume=None)))
        await asyncio.sleep(0)
        turn = self._turn([_m("user", "早")], "在吗")
        handle.queue.put_nowait(turn)
        await asyncio.wait_for(task, timeout=2)   # 外泄取消：收摊返回，不外抛
        self.assertIn("engine-error", handle.stop_reason or "")
        # 被取消的轮也有结局：error+done+None
        chunks = []
        while not turn.out.empty():
            chunks.append(turn.out.get_nowait())
        blob = b"".join(c for c in chunks if c)
        self.assertIn(b'"type": "error"', blob)


class ChatEngineConfigTest(unittest.TestCase):
    """CHAT_ENGINE 灰度解析（config.chat_engine）。"""

    def test_gray_parsing(self):
        import config
        orig = config.CHAT_ENGINE
        try:
            config.CHAT_ENGINE = "p"
            self.assertEqual(config.chat_engine("cass"), "p")
            config.CHAT_ENGINE = "sdk"
            self.assertEqual(config.chat_engine("cass"), "sdk")
            config.CHAT_ENGINE = "sdk:default"
            self.assertEqual(config.chat_engine("default"), "sdk")
            self.assertEqual(config.chat_engine("cass"), "p")
            config.CHAT_ENGINE = "sdk:default, cass"
            self.assertEqual(config.chat_engine("cass"), "sdk")
        finally:
            config.CHAT_ENGINE = orig


if __name__ == "__main__":
    unittest.main()
