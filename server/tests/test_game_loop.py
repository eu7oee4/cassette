"""game_loop（PLAN_sdk S1/PR5）单测：分段回传语义、队列注入、收摊三条路。
引擎用假 client（脚本化消息流），不打真模型、不碰模拟器。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_game_loop -v
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import game_loop
import session_mgr as sm
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock


def _asst(*blocks):
    return AssistantMessage(content=list(blocks), model="test")


def _result():
    return ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0,
                         is_error=False, num_turns=1, session_id="s")


class FakeClient:
    """脚本化引擎：feed() 塞事件，close_stream() 结束 receive_messages。"""

    def __init__(self, options):
        self.options = options
        self.queries: list[str] = []
        self.disconnected = False
        self._q: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        pass

    async def query(self, text):
        self.queries.append(text)

    def feed(self, *msgs):
        for m in msgs:
            self._q.put_nowait(m)

    def close_stream(self):
        self._q.put_nowait(None)

    async def receive_messages(self):
        while True:
            m = await self._q.get()
            if m is None:
                return
            yield m

    async def disconnect(self):
        self.disconnected = True


class GameLoopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sm.set_event_loop(asyncio.get_running_loop())
        sm._registry.clear()
        self._pause_orig = game_loop.TICK_PAUSE_SEC
        self._stable_orig = game_loop._screen_stable
        game_loop.TICK_PAUSE_SEC = 0            # 测试不等 tick 停顿
        game_loop._screen_stable = lambda: True  # 别在单测里打 adb
        self.handle = sm.LoopHandle(char_id="cass", scene="game")
        self.delivered: list[tuple[str, bool]] = []
        self.closed: list = []
        self.client: FakeClient | None = None

        def factory(options):
            self.client = FakeClient(options)
            return self.client

        self.task = asyncio.create_task(game_loop.run(
            self.handle, context_text="〔开场〕", deliver=self._deliver,
            options=object(), client_factory=factory,
            on_closed=self.closed.append))
        await asyncio.sleep(0.01)

    def _deliver(self, text, stop):
        self.delivered.append((text, stop))

    async def asyncTearDown(self):
        game_loop.TICK_PAUSE_SEC = self._pause_orig
        game_loop._screen_stable = self._stable_orig
        if not self.task.done():
            self.task.cancel()
            await asyncio.sleep(0.01)

    async def test_opening_segments_then_tick(self):
        """开场进 query；工具间正文=seg、轮尾=stop；轮尾队列空 → 自动补 tick。"""
        self.assertEqual(self.client.queries, ["〔开场〕"])
        self.client.feed(
            _asst(TextBlock(text="先看一眼画面"),
                  ToolUseBlock(id="t1", name="mcp__game__game_look", input={})),
            _asst(TextBlock(text="这句台词写得妙")),
            _asst(TextBlock(text="接着读下一段"),
                  ToolUseBlock(id="t2", name="mcp__game__game_tap", input={})),
            _asst(TextBlock(text="今天读到这儿")),
            _result(),
        )
        await asyncio.sleep(0.05)
        self.assertEqual(self.delivered, [
            ("先看一眼画面", False),
            ("这句台词写得妙", False),
            ("接着读下一段", False),
            ("今天读到这儿", True),
        ])
        self.assertEqual(self.client.queries,
                         ["〔开场〕", game_loop.TICK_PROMPT])   # 续弹

    async def test_queue_beats_tick(self):
        """轮尾队列非空：TA 的话优先于 tick（插入延迟=一个短轮）。"""
        self.handle.queue.put_nowait("〔现在是 12:00〕\n继续吧")
        self.client.feed(_asst(TextBlock(text="嗯")), _result())
        await asyncio.sleep(0.05)
        self.assertEqual(self.client.queries,
                         ["〔开场〕", "〔现在是 12:00〕\n继续吧"])
        self.assertNotIn(game_loop.TICK_PROMPT, self.client.queries)

    async def test_end_requested_closes_after_turn(self):
        self.handle.meta["end_requested"] = True                # game_end 工具置的旗
        self.client.feed(_asst(TextBlock(text="收摊前道个别")), _result())
        await asyncio.sleep(0.05)
        self.assertTrue(self.task.done())
        self.assertEqual(self.delivered, [("收摊前道个别", True)])
        self.assertTrue(self.client.disconnected)
        self.assertEqual(self.closed, [self.handle])
        self.assertNotIn(game_loop.TICK_PROMPT, self.client.queries)  # 收摊不再续弹

    async def test_cancel_cleans_up(self):
        self.task.cancel()
        await asyncio.sleep(0.05)
        self.assertTrue(self.client.disconnected)
        self.assertEqual(self.closed, [self.handle])

    async def test_stream_eof_exits_with_reason(self):
        """引擎消息流断了 = 引擎死了：要带 stop_reason 退出，不能当轮结束空转。"""
        self.client._q.put_nowait("not-a-message")   # 非 Message 对象要被安静跳过
        self.client.close_stream()
        await asyncio.sleep(0.05)
        self.assertTrue(self.task.done())
        self.assertIn("引擎", self.handle.stop_reason or "")
        self.assertEqual(self.closed, [self.handle])


class ReopenTest(unittest.IsolatedAsyncioTestCase):
    """滚动重开（PR6+tick）：攒够截图 → 稳定断言 → 写进度页 → 铸文本史 →
    新 client resume → ping → 续弹。"""

    async def asyncSetUp(self):
        sm.set_event_loop(asyncio.get_running_loop())
        self.handle = sm.LoopHandle(char_id="cass", scene="game")
        self.clients: list[FakeClient] = []
        self.delivered: list[tuple[str, bool]] = []
        self.forged: list = []
        self._render_orig = game_loop.forge.render
        self._pause_orig = game_loop.TICK_PAUSE_SEC
        self._stable_orig = game_loop._screen_stable
        game_loop.TICK_PAUSE_SEC = 0
        game_loop._screen_stable = lambda: True

        def fake_render(messages, **kw):
            self.forged.append((list(messages), kw))
            return "sid-forged"
        game_loop.forge.render = fake_render

        def factory(options):
            c = FakeClient(options)
            self.clients.append(c)
            return c

        self.task = asyncio.create_task(game_loop.run(
            self.handle, context_text="〔开场〕",
            deliver=lambda t, s: self.delivered.append((t, s)),
            options=type("O", (), {"cwd": "/tmp/game-cwd"})(),
            client_factory=factory, reopen_shots=2))
        await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        game_loop.forge.render = self._render_orig
        game_loop.TICK_PAUSE_SEC = self._pause_orig
        game_loop._screen_stable = self._stable_orig
        if not self.task.done():
            self.task.cancel()
            await asyncio.sleep(0.01)

    async def test_reopen_flow(self):
        c1 = self.clients[0]
        self.handle.meta["shots"] = 2                     # 工具层计的账，测试直填
        c1.feed(_asst(TextBlock(text="这章读完了")), _result())
        await asyncio.sleep(0.05)
        # ① 巩固钩子：感知白描提示（无感化：不出现「重开」，是他自己的念头）
        self.assertEqual(len(c1.queries), 2)
        self.assertIn("进度记一笔", c1.queries[1])
        self.assertNotIn("重开", c1.queries[1])
        self.assertNotIn("系统", c1.queries[1])
        c1.feed(_asst(TextBlock(text="记好了")), _result())   # 他更新完进度页
        await asyncio.sleep(0.05)
        # ② 铸文本史：user 开场 + 他说过的话，全是 TA 见过的原文（tick 不在里面）
        self.assertEqual(len(self.forged), 1)
        msgs, kw = self.forged[0]
        self.assertEqual(kw.get("cwd"), "/tmp/game-cwd")
        self.assertEqual([m["role"] for m in msgs],
                         ["user", "assistant", "assistant"])
        self.assertEqual(msgs[1]["text"], "这章读完了")
        # ③ 新 client resume 了铸出的 sid，首个 tick 即 ping（没有自检文案）
        self.assertEqual(len(self.clients), 2)
        c2 = self.clients[1]
        self.assertEqual(c2.options.resume, "sid-forged")
        self.assertEqual(c2.queries[0], game_loop.TICK_PROMPT)
        c2.feed(_asst(TextBlock(text="接上了，继续读")), _result())
        await asyncio.sleep(0.05)
        self.assertFalse(self.handle.reopening)
        self.assertEqual(self.handle.meta["shots"], 0)
        # ④ ping 轮是真轮，说的话正常上屏；收完继续续弹
        texts = [t for t, _ in self.delivered]
        self.assertIn("记好了", texts)
        self.assertIn("接上了，继续读", texts)
        self.assertEqual(c2.queries[1], game_loop.TICK_PROMPT)
        # ⑤ 重开后 TA 的消息进的是新 client（tick 轮收完后轮到它）
        self.handle.queue.put_nowait("继续")
        c2.feed(_asst(TextBlock(text="嗯")), _result())       # tick 轮收掉
        await asyncio.sleep(0.05)
        self.assertIn("继续", c2.queries)
        self.assertNotIn("继续", c1.queries)

    async def test_reopen_deferred_until_stable(self):
        """稳定性断言：没定格就推迟；连推 REOPEN_MAX_DEFERS 次后不再等。"""
        game_loop._screen_stable = lambda: False           # 画面永远在动
        defer_orig = game_loop.REOPEN_DEFER_SHOTS
        game_loop.REOPEN_DEFER_SHOTS = 0                   # 阈值不抬，专测推迟计数
        try:
            c1 = self.clients[0]
            self.handle.meta["shots"] = 2
            c1.feed(_asst(TextBlock(text="一")), _result())    # 第 1 次：推迟
            await asyncio.sleep(0.05)
            self.assertEqual(self.handle.meta["reopen_defers"], 1)
            self.assertEqual(self.forged, [])
            c1.feed(_asst(TextBlock(text="二")), _result())    # 第 2 次：再推迟
            await asyncio.sleep(0.05)
            self.assertEqual(self.handle.meta["reopen_defers"], 2)
            self.assertEqual(self.forged, [])
            c1.feed(_asst(TextBlock(text="三")), _result())    # 第 3 次：不再等，重铸
            await asyncio.sleep(0.05)
            self.assertEqual(len(c1.queries) and len(self.clients), 1)  # 还在巩固轮
            self.assertIn("进度记一笔", c1.queries[-1])
        finally:
            game_loop.REOPEN_DEFER_SHOTS = defer_orig

    async def test_reopen_ping_fail_retries_then_dies(self):
        c1 = self.clients[0]
        self.handle.meta["shots"] = 2
        game_loop_timeout = game_loop.REOPEN_PING_TIMEOUT
        game_loop.REOPEN_PING_TIMEOUT = 0.05              # ping 永远等不到
        try:
            c1.feed(_asst(TextBlock(text="读完了")), _result())
            await asyncio.sleep(0.02)
            c1.feed(_result())                             # 巩固轮直接结束
            await asyncio.wait_for(self.task, 2)           # 首试+重试都失败 → 收摊
        except asyncio.TimeoutError:
            self.fail("重开失败后 loop 没退出")
        finally:
            game_loop.REOPEN_PING_TIMEOUT = game_loop_timeout
        self.assertEqual(len(self.clients), 3)             # 旧 + 首试 + 重试
        self.assertIn("engine-error", self.handle.stop_reason or "")


class GameServerBuildTest(unittest.TestCase):
    def test_build_game_server_smoke(self):
        h = sm.LoopHandle(char_id="cass", scene="game")
        cfg = game_loop.build_game_server(h)
        self.assertEqual(cfg.get("type"), "sdk")
        self.assertIsNotNone(cfg.get("instance"))


if __name__ == "__main__":
    unittest.main()
