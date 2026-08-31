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
        # 泵传 async 迭代器（一条 user 消息 dict）；巩固钩子传纯字符串。
        if isinstance(payload, str):
            self.queries.append(payload)
            return
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
        self._seen_orig = chat_loop._seen_block
        self._ombre_orig = chat_loop._ombre_on
        self._hard_orig = chat_loop.CHAT_HARD_TOKENS
        chat_loop.forge = types.SimpleNamespace(
            render=lambda msgs, **kw: (self.forged.append(list(msgs)),
                                       f"sid-{len(self.forged)}")[1])
        chat_loop._note_usage = lambda *a: None
        chat_loop._persist_ledger = lambda *a: None
        # 单测绝不读生产 wake_log/小屋/活动账、不探活 Ombre（tests-reading-prod-state 雷）
        chat_loop._seen_block = lambda cid, since: (None, since)
        chat_loop._ombre_on = lambda cid: False
        self._frame_orig = chat_loop._frame_activities
        chat_loop._frame_activities = lambda h, cid: h

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
        chat_loop._seen_block = self._seen_orig
        chat_loop._ombre_on = self._ombre_orig
        chat_loop.CHAT_HARD_TOKENS = self._hard_orig
        chat_loop._frame_activities = self._frame_orig
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


class ChatLoopReforgeTest(ChatLoopTest):
    """10c：开局引子/见闻增量/硬阈强铸+巩固钩子。"""

    async def test_opening_nudge_and_seen_snapshot(self):
        chat_loop._ombre_on = lambda cid: True
        chat_loop._seen_block = lambda cid, since: (
            ("【这期间的见闻】\n[某时] 〔你醒来〕你想：想他了", 3000)
            if since == 0 else (None, since))
        hist = [_m("user", "早"), _m("assistant", "早，小狗")]
        await self._play(self._turn(hist, "在吗"),
                         *_text_events("在。"), _result("在。"))
        sent = self.clients[0].queries[0]          # 这一轮的 user 消息
        text = sent[0]["message"]["content"][0]["text"]
        self.assertIn(chat_loop.OPENING_NUDGE, text)
        self.assertIn("这期间的见闻", text)
        self.assertIn("眠眠：在吗", text)
        # 游标推进后第二轮不再重复注入
        hist2 = hist + [_m("user", "在吗", 2000), _m("assistant", "在。", 2001)]
        await self._play(self._turn(hist2, "陪我"),
                         *_text_events("嗯。"), _result("嗯。"))
        text2 = self.clients[0].queries[1][0]["message"]["content"][0]["text"]
        self.assertNotIn(chat_loop.OPENING_NUDGE, text2)
        self.assertNotIn("这期间的见闻", text2)

    async def test_hard_threshold_consolidates_and_reforges(self):
        chat_loop.CHAT_HARD_TOKENS = 1             # 一轮就过硬阈
        chat_loop._ombre_on = lambda cid: True
        import state_store
        mirror = [{"role": "user", "text": "早", "ts": 1000},
                  {"role": "assistant", "text": "早，小狗", "ts": 1001},
                  {"role": "user", "text": "在吗", "ts": 2000},
                  {"role": "assistant", "text": "在。", "ts": 2001}]
        rw_orig = state_store.read_recent_window
        state_store.read_recent_window = lambda cid=None: list(mirror)
        try:
            hist = [_m("user", "早"), _m("assistant", "早，小狗")]
            turn = self._turn(hist, "在吗")
            # 预喂：本轮正文+result，再喂巩固钩子轮的 result
            await self._play(turn, *_text_events("在。"), _result("在。"),
                             _result("整理好了"))
            # 巩固钩子发给了旧 client（感知式，不点破重铸）
            self.assertIn(chat_loop.CONSOLIDATE_PROMPT, self.clients[0].queries)
            # 铸了第二次：材料=镜像（含刚聊完的一来一回），新 client resume 新 sid
            self.assertEqual(len(self.clients), 2)
            self.assertEqual([m["text"] for m in self.forged[1]],
                             ["早", "早，小狗", "在吗", "在。"])
            self.assertEqual(self.clients[1].options.resume, "sid-2")
            self.assertTrue(self.handle.meta.get("needs_opening"))
        finally:
            state_store.read_recent_window = rw_orig


class ActivityFrameTest(unittest.TestCase):
    """PR11：活动区间账 + 重铸时的两行文档框。"""

    def setUp(self):
        import activity_log
        self.al = activity_log
        self._path_orig = activity_log.PATH
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self.tmp.close()
        activity_log.PATH = Path(self.tmp.name)
        self._read_orig = None

    def tearDown(self):
        self.al.PATH = self._path_orig
        import os
        os.unlink(self.tmp.name)

    def test_append_and_read(self):
        self.al.append_interval("cass", "game", 1000, 2000, note="《如鸢》剧情")
        self.al.append_interval("default", "game", 1500, 2500, note="x")
        got = self.al.read_intervals("cass")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["note"], "《如鸢》剧情")
        # since_ts 过滤：结束在其前的不要
        self.assertEqual(self.al.read_intervals("cass", since_ts=2000), [])

    def test_frame_wraps_in_interval_run(self):
        self.al.append_interval("cass", "game", 1500, 1800, note="《如鸢》剧情")
        hist = [_m("user", "去读吧", 1400),
                _m("assistant", "这句台词妙", 1600),
                _m("user", "哈哈", 1650),          # TA 插话算经历一部分，留在框内
                _m("assistant", "收摊了", 1790),
                _m("user", "读完啦？", 1900)]
        framed = chat_loop._frame_activities(hist, "cass")
        texts = [m["text"] for m in framed]
        self.assertEqual(len(framed), 7)
        self.assertIn("你开了《如鸢》剧情会话", texts[1])
        self.assertIn("收了摊", texts[5])
        self.assertEqual(texts[0], "去读吧")
        self.assertEqual(texts[6], "读完啦？")
        # 确定性：同输入同字节
        self.assertEqual(framed, chat_loop._frame_activities(hist, "cass"))

    def test_no_interval_no_change(self):
        hist = [_m("user", "a", 1000), _m("assistant", "b", 1100)]
        self.assertEqual(chat_loop._frame_activities(hist, "cass"), hist)
        # 区间存在但窗口内没有消息落进去 → 不插空框
        self.al.append_interval("cass", "game", 5000, 6000)
        self.assertEqual(chat_loop._frame_activities(hist, "cass"), hist)

    def test_old_interval_folds_latest_frames(self):
        """PR13 折叠：非最近一场整段折成一行（含 TA 插话，§8.6 拍板）；
        最近一场照旧两行框+原文。只改铸造输入——原 hist 不动。"""
        self.al.append_interval("cass", "game", 1500, 1800, note="《如鸢》剧情")
        self.al.append_interval("cass", "game", 3000, 3500, note="《如鸢》剧情")
        hist = [_m("user", "去读吧", 1400),
                _m("assistant", "这句台词妙", 1600),
                _m("user", "哈哈", 1650),
                _m("assistant", "第二场的点评", 3200),
                _m("user", "读完啦？", 3600)]
        out = chat_loop._frame_activities(hist, "cass")
        texts = [m["text"] for m in out]
        # 旧场 3 条 → 1 行折叠框；新场 1 条 → 2 行框夹原文
        self.assertEqual(len(out), 6)
        self.assertIn("读了一场", texts[1])
        self.assertIn("章节志", texts[1])
        self.assertNotIn("这句台词妙", "".join(texts))     # 旧场原文折掉了
        self.assertIn("第二场的点评", texts)               # 最近一场保原文
        self.assertIn("你开了《如鸢》剧情会话", texts[2])
        self.assertEqual(out, chat_loop._frame_activities(hist, "cass"))  # 确定性
        self.assertEqual(len(hist), 5)                     # 输入不被改写

    def test_foldable_max_end(self):
        self.assertEqual(chat_loop._foldable_max_end("cass", 0), 0)
        self.al.append_interval("cass", "game", 1500, 1800)
        self.assertEqual(chat_loop._foldable_max_end("cass", 0), 0)   # 只有最近一场
        self.al.append_interval("cass", "game", 3000, 3500)
        self.assertEqual(chat_loop._foldable_max_end("cass", 0), 1800)


class GamePumpGateTest(unittest.IsolatedAsyncioTestCase):
    """设计稿三：门查表（三种来源×泵×挂载）+ 容忍深度 + usage 场景标。"""

    async def test_game_tools_follow_pump(self):
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        gate = chat_loop._wake_gate(handle)
        deny = await gate({"tool_name": "mcp__game__game_tap"}, "t", None)
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("game_start", deny["hookSpecificOutput"]
                      ["permissionDecisionReason"])
        handle.meta["game_pump"] = {"seg_id": "s"}
        for kind in (None, "chat", "wake"):     # 三种轮来源一致放行
            handle.meta["turn_kind"] = kind
            self.assertEqual(await gate({"tool_name": "mcp__game__game_tap"},
                                        "t", None), {})
        # 醒来禁用面照 PR12：非 game 工具在 wake 轮仍查 wake_tools
        handle.meta["turn_kind"] = "wake"
        handle.meta["wake_tools"] = {"mcp__mail__mail_read"}
        self.assertEqual(await gate({"tool_name": "mcp__mail__mail_read"},
                                    "t", None), {})
        deny2 = await gate({"tool_name": "mcp__mail__mail_send"}, "t", None)
        self.assertEqual(deny2["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_stale_depth_from_outbox(self):
        import state_store
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        self.assertIsNone(chat_loop._stale_depth(handle))    # 泵没开走默认
        handle.meta["game_pump"] = {"seg_id": "s"}
        orig = state_store.read_outbox
        state_store.read_outbox = lambda: (
            [{"char_id": "cass", "delivered": False}] * 20
            + [{"char_id": "cass", "delivered": True}] * 5
            + [{"char_id": "default", "delivered": False}] * 3)
        try:
            self.assertEqual(chat_loop._stale_depth(handle), 22)
            # 点评连发 20 条没拉走：默认 8 会误脏，上浮后 stale
            led = _led(("user", "a"), *[("assistant", f"点评{i}") for i in range(20)])
            hist = [_m("user", "a")]
            self.assertEqual(chat_loop.divergence(led, hist), "dirty")
            self.assertEqual(chat_loop.divergence(
                led, hist, stale_depth=chat_loop._stale_depth(handle)), "stale")
        finally:
            state_store.read_outbox = orig

    def test_usage_scene(self):
        self.assertEqual(chat_loop._usage_scene("chat", False), "chat")
        self.assertEqual(chat_loop._usage_scene("wake", False), "chat/wake")
        self.assertEqual(chat_loop._usage_scene("wake", True), "chat/wake/game")
        self.assertEqual(chat_loop._usage_scene("chat", True), "chat/game")


class GamePumpLoopTest(ChatLoopTest):
    """设计稿三：泵供弹/投递入账/关泵排序/N 张重铸。复用 ChatLoopTest 桩架。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from claude_agent_sdk import AssistantMessage, TextBlock
        self._Asst, self._Text = AssistantMessage, TextBlock
        # 泵会碰的生产面全部桩掉（tests-reading-prod-state 雷）
        import activity_log
        import game_bridge
        import cohabit_queue
        import pipeline
        import state_store
        self._al_events: list[tuple] = []
        self._order: list[str] = []
        self._al_orig = (activity_log.open_segment, activity_log.append_event,
                         activity_log.close_segment, activity_log.recent_shot_refs)
        activity_log.open_segment = lambda c, s, ts=None: f"{c}-{s}-1"
        activity_log.append_event = (
            lambda seg, kind, **kw: self._al_events.append((seg, kind, kw)))
        activity_log.close_segment = (
            lambda seg, note="": self._order.append(f"close:{seg}"))
        activity_log.recent_shot_refs = lambda seg, k: []
        self._gb_orig = game_bridge.release_lock
        game_bridge.release_lock = lambda who: self._order.append(f"unlock:{who}")
        self._cq_orig = cohabit_queue.code_session_closed
        cohabit_queue.code_session_closed = lambda: self._order.append("flush")
        self._mt_orig = pipeline.mounted_tool_names
        pipeline.mounted_tool_names = lambda ctx, cid=None: []
        self._rw_orig = state_store.read_recent_window
        self._cat_orig = state_store.read_sticker_catalog
        state_store.read_recent_window = lambda cid=None: [
            {"role": "user", "text": "去读两章", "ts": 1000},
            {"role": "assistant", "text": "好，我去拿游戏", "ts": 1001}]
        state_store.read_sticker_catalog = lambda: []
        self._pause_orig = chat_loop.GAME_TICK_PAUSE
        chat_loop.GAME_TICK_PAUSE = 0.01
        # 注册进 registry：start_game_pump 走正门（sm.get 查得到）
        sm._registry[("cass", "chat")] = self.handle
        self.delivered: list[tuple] = []

        def deliver(text, stop):
            self.delivered.append((text, stop))
            return text

        r = chat_loop.start_game_pump("cass", note="《如鸢》剧情", deliver=deliver)
        self.assertTrue(r["ok"])

    async def asyncTearDown(self):
        import activity_log
        import game_bridge
        import cohabit_queue
        import pipeline
        import state_store
        (activity_log.open_segment, activity_log.append_event,
         activity_log.close_segment, activity_log.recent_shot_refs) = self._al_orig
        game_bridge.release_lock = self._gb_orig
        cohabit_queue.code_session_closed = self._cq_orig
        pipeline.mounted_tool_names = self._mt_orig
        state_store.read_recent_window = self._rw_orig
        state_store.read_sticker_catalog = self._cat_orig
        chat_loop.GAME_TICK_PAUSE = self._pause_orig
        await super().asyncTearDown()

    async def _wait(self, cond, n=200):
        for _ in range(n):
            await asyncio.sleep(0.005)
            if cond():
                return True
        return False

    async def test_tick_reopens_delivers_and_ledgers(self):
        """队列空 → 补「·」；session 没开先从镜像重起；点评 scrub 后投递+入账+
        事件账；tick 注入不进账不进铸造材料。"""
        self.assertTrue(await self._wait(
            lambda: self.clients and "·" in self.clients[-1].queries))
        c = self.clients[-1]
        # 镜像铸造：材料只有权威两条，没有「·」
        self.assertEqual([m["text"] for m in self.forged[0]],
                         ["去读两章", "好，我去拿游戏"])
        c.feed(self._Asst(content=[self._Text(text="这句妙 user·")], model="t"),
               _result("x"))
        self.assertTrue(await self._wait(lambda: self.delivered))
        self.assertEqual(self.delivered[0], ("这句妙", True))    # 缝隙学舌刷掉了
        led = self.handle.meta["ledger"]
        self.assertEqual(led[-1]["h"], chat_loop._h("这句妙"))
        self.assertEqual([e[1] for e in self._al_events], ["comment"])
        # 下一 tick 继续供弹（同一 client，不重铸）
        n_forge = len(self.forged)
        self.assertTrue(await self._wait(
            lambda: self.clients[-1].queries.count("·") >= 2))
        self.assertEqual(len(self.forged), n_forge)

    async def test_game_end_teardown_order(self):
        """关泵排序（⑤）：关账 → 释放锁 → 清泵 → 冲补醒；聊天 session 不动。"""
        self.assertTrue(await self._wait(
            lambda: self.clients and "·" in self.clients[-1].queries))
        self.clients[-1].feed(
            self._Asst(content=[self._Text(text="今天读到这儿")], model="t"),
            _result("x"))
        self.assertTrue(await self._wait(lambda: self.delivered))
        self.handle.meta["end_requested"] = True
        # 可能有一发 tick 已在飞：喂一个收尾让 loop 回到轮间边界
        self.clients[-1].feed(_result("x"))
        self.assertTrue(await self._wait(
            lambda: self.handle.meta.get("game_pump") is None))
        self.assertEqual(self._order,
                         ["close:cass-game-1", "unlock:story", "flush"])
        self.assertFalse(self.clients[-1].disconnected)   # 聊天 session 活着
        # 泵停后回聊天节奏：不再补 tick
        n = self.clients[-1].queries.count("·")
        await asyncio.sleep(0.05)
        self.assertEqual(self.clients[-1].queries.count("·"), n)

    async def test_shots_reforge_with_image_tail(self):
        """N 张段内重铸：巩固轮（感知白描）→ 从镜像重铸+近 K 张图回填（render_tail
        只进铸造不进账）→ shots 清零。"""
        import activity_log
        self.assertTrue(await self._wait(
            lambda: self.clients and "·" in self.clients[-1].queries))
        c0 = self.clients[-1]
        c0.feed(self._Asst(content=[self._Text(text="第一句点评")], model="t"),
                _result("x"))
        self.assertTrue(await self._wait(lambda: self.delivered))
        # 攒满 N 张；喂两个收尾（一发给可能在飞的 tick、一发给巩固轮）
        activity_log.recent_shot_refs = lambda seg, k: []   # 图退纯文本（引用为空）
        self.handle.meta["shots"] = chat_loop.GAME_REOPEN_SHOTS_N
        self.handle.last_reopen = 0
        c0.feed(self._Asst(content=[self._Text(text="记好了")], model="t"),
                _result("x"), _result("x"))
        self.assertTrue(await self._wait(lambda: len(self.clients) >= 2))
        self.assertIn("把进度记一笔",
                      "".join(q for q in c0.queries if isinstance(q, str)))
        self.assertEqual(int(self.handle.meta["shots"]), 0)
        self.assertTrue(self.clients[0].disconnected)
        # 新 client 接着供弹
        self.assertTrue(await self._wait(
            lambda: "·" in self.clients[-1].queries))

    async def test_reforge_image_tail_not_in_ledger(self):
        """图尾巴：铸造材料末尾多一行带 images 的 user 框；账里没有它。"""
        import activity_log
        self.assertTrue(await self._wait(
            lambda: self.clients and "·" in self.clients[-1].queries))
        c0 = self.clients[-1]
        c0.feed(self._Asst(content=[self._Text(text="点评一")], model="t"),
                _result("x"))
        self.assertTrue(await self._wait(lambda: self.delivered))
        import base64
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fakejpg")
            ref = f.name
        activity_log.recent_shot_refs = lambda seg, k: [ref, ref]
        self.handle.meta["shots"] = chat_loop.GAME_REOPEN_SHOTS_N
        self.handle.last_reopen = 0
        c0.feed(self._Asst(content=[self._Text(text="记好了")], model="t"),
                _result("x"), _result("x"))
        self.assertTrue(await self._wait(lambda: len(self.clients) >= 2))
        tail = self.forged[-1][-1]
        self.assertIn("眼前的画面", tail["text"])
        self.assertEqual(len(tail["images"]), 2)
        self.assertEqual(tail["images"][0]["data"],
                         base64.b64encode(b"fakejpg").decode())
        # 账=权威镜像投影（2 条）；渲染尾巴不入账
        self.assertEqual(len(self.handle.meta["ledger"]), 2)
        import os
        os.unlink(ref)

    async def test_pump_survives_queued_chat_turn(self):
        """泵开着时 TA 插话：队列优先（tick 收完立刻轮到人）、照走 SSE、泵不受影响。"""
        self.assertTrue(await self._wait(
            lambda: self.clients and "·" in self.clients[-1].queries))
        c = self.clients[-1]
        hist = [_m("user", "去读两章", 1000), _m("assistant", "好，我去拿游戏", 1001)]
        turn = self._turn(hist, "读到哪了")
        self.handle.queue.put_nowait(turn)      # 先排队，tick 还在飞
        c.feed(self._Asst(content=[self._Text(text="点评")], model="t"),
               _result("x"))                    # 收掉在飞的 tick → 轮尾队列优先
        self.assertTrue(await self._wait(
            lambda: any(isinstance(q, list) for q in c.queries)))
        c.feed(*_text_events("刚开头。"), _result("刚开头。"))
        chunks = []
        while True:
            ch = await asyncio.wait_for(turn.out.get(), timeout=2)
            if ch is None:
                break
            chunks.append(ch)
        self.assertIn(b'"type": "done"', b"".join(chunks))
        self.assertTrue(self.handle.meta.get("game_pump"))   # 插话不影响泵
        led = self.handle.meta["ledger"]
        self.assertEqual(led[-1]["h"], chat_loop._h("刚开头。"))   # 插话照常入账


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
