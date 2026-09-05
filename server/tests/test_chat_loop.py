"""chat_loop（PLAN_sdk S2/PR10）单测：判脏矩阵、铸造/重铸触发、轮结局、收摊纪律。
引擎用假 client（脚本化消息流），forge 用桩（不碰 ~/.claude）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_chat_loop -v
"""
import asyncio
import json
import os
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


def _stamp(text, ts=1000):
    """_stamp_times 打完戳的样子（渲染层给 TA 的每句话加时间锚——历史无时间戳
    曾让模型把 3 分钟前说的话说成「昨天」，2026-09-02）。"""
    return chat_loop._stamp_times([{"role": "user", "text": text, "ts": ts}])[0]["text"]


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

    def test_image_placeholder_tail_absorbed_not_dirty(self):
        """图+文一起发：app 长两条气泡，账只记一条 new_msg → 窗口比账长 → 从前
        每次必判脏必全量重铸（2026-09-02）。占位符的真内容随 req.images 已经到他
        眼前了，账补记一笔就行。"""
        led = _led(("user", "a"), ("assistant", "b"))
        hist = [_m("user", "a"), _m("assistant", "b"),
                _m("user", "[图片]", 2000)]
        self.assertEqual(chat_loop.divergence(led, hist), "dirty")   # 旧行为仍是脏
        tail = chat_loop.absorbable_tail(led, hist)
        self.assertEqual([m["text"] for m in tail], ["[图片]"])
        core = hist[:len(hist) - len(tail)]
        self.assertIsNone(chat_loop.divergence(led, core))           # 摘掉就干净
        # 三张图一次发（09-01 20:52 的真实形状）
        hist3 = hist + [_m("user", "[图片]", 2000), _m("user", "[图片]", 2000)]
        self.assertEqual(len(chat_loop.absorbable_tail(led, hist3)), 3)

    def test_absorb_only_swallows_placeholders(self):
        """分寸：只吸收占位符。别的多出来的消息=有话他没见过，那种必须重铸——
        不能为了省一次重铸把「账上有、他没见过」做成常态。"""
        led = _led(("user", "a"), ("assistant", "b"))
        # 真的一句话，不吸收
        self.assertEqual(chat_loop.absorbable_tail(
            led, [_m("user", "a"), _m("assistant", "b"),
                  _m("user", "还有个事", 2000)]), [])
        # 他自己的话不吸收（assistant 一定是随轮进账的）
        self.assertEqual(chat_loop.absorbable_tail(
            led, [_m("user", "a"), _m("assistant", "b"),
                  _m("assistant", "多的", 2000)]), [])
        # ts 没往前走 = 编辑不是追加
        self.assertEqual(chat_loop.absorbable_tail(
            led, [_m("user", "a"), _m("assistant", "b"),
                  _m("user", "[图片]", 1000)]), [])
        # 账空 / 窗空
        self.assertEqual(chat_loop.absorbable_tail([], [_m("user", "[图片]")]), [])

    def test_dirty_reason_names_the_cause(self):
        """重铸是最重的动作（换 client、缓存作废、注入全蒸发），日志不能只说
        「判脏」不说宾语（2026-09-02 查了半小时才定位到是一张图）。"""
        led = _led(("user", "a"), ("assistant", "b"))
        # 图+文：一次发送落成两条气泡，账只记了一条 new_msg → 窗口比账长
        longer = [_m("user", "a"), _m("assistant", "b"), _m("user", "[图片]")]
        self.assertEqual(chat_loop.divergence(led, longer), "dirty")
        self.assertIn("比账", chat_loop.dirty_reason(led, longer))
        self.assertIn("图+文", chat_loop.dirty_reason(led, longer))
        # TA 编辑历史 → 点名第几条
        edited = [_m("user", "a"), _m("assistant", "改过的话")]
        self.assertIn("第 1 条对不上", chat_loop.dirty_reason(led, edited))
        self.assertIn("改过的话", chat_loop.dirty_reason(led, edited))
        # 账空 / 窗口空
        self.assertIn("账是空的", chat_loop.dirty_reason([], longer))
        self.assertIn("app 清了历史", chat_loop.dirty_reason(led, []))

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
        self._ombre_orig = chat_loop._ombre_on
        self._hard_orig = chat_loop.CHAT_HARD_TOKENS
        chat_loop.forge = types.SimpleNamespace(
            render=lambda msgs, **kw: (self.forged.append(list(msgs)),
                                       f"sid-{len(self.forged)}")[1])
        chat_loop._note_usage = lambda *a: None
        chat_loop._persist_ledger = lambda *a: None
        # 单测绝不读生产活动账、不探活 Ombre（tests-reading-prod-state 雷）
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
        self.assertEqual([m["text"] for m in self.forged[0]],
                         [_stamp("早"), "早，小狗"])   # TA 的话带时间锚，他自己的不带
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
                         [_stamp("早"), "改过的话", _stamp("在吗", 2000), "在。"])

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
    """10c：开局引子/硬阈强铸+巩固钩子。（见闻快照 09-02 删了：注入块只剩
    breath 引子；seen 相关断言随之退役。）"""

    async def test_opening_nudge(self):
        chat_loop._ombre_on = lambda cid: True
        hist = [_m("user", "早"), _m("assistant", "早，小狗")]
        await self._play(self._turn(hist, "在吗"),
                         *_text_events("在。"), _result("在。"))
        sent = self.clients[0].queries[0]          # 这一轮的 user 消息
        text = sent[0]["message"]["content"][0]["text"]
        self.assertIn(chat_loop.OPENING_NUDGE, text)
        self.assertIn("眠眠：在吗", text)
        # 开局旗消耗后第二轮不再重复注入
        hist2 = hist + [_m("user", "在吗", 2000), _m("assistant", "在。", 2001)]
        await self._play(self._turn(hist2, "陪我"),
                         *_text_events("嗯。"), _result("嗯。"))
        text2 = self.clients[0].queries[1][0]["message"]["content"][0]["text"]
        self.assertNotIn(chat_loop.OPENING_NUDGE, text2)

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
                             [_stamp("早"), "早，小狗", _stamp("在吗", 2000), "在。"])
            self.assertEqual(self.clients[1].options.resume, "sid-2")
            self.assertTrue(self.handle.meta.get("needs_opening"))
        finally:
            state_store.read_recent_window = rw_orig



class ChatImageMergeTest(unittest.TestCase):
    """并图（2026-09-02）：TA 发的图从前只活到下一次重铸——历史里只剩 `[图片]`
    三个字（-p 时期遗留：那会儿历史是扁平文本，图只能当占位符）。留底 + 渲染层
    并图之后，重铸出来是一条 user 槽 = 那句话 + 真图块。"""

    def setUp(self):
        import base64
        import state_store
        import tempfile
        self.b64 = base64.b64encode(b"JPEGBYTES").decode()
        self.ss = state_store
        self._root_orig = state_store.CHAR_STATE_ROOT
        self.dir = Path(tempfile.mkdtemp())
        state_store.CHAR_STATE_ROOT = self.dir      # 别写进真 state
        self.cwd = str(self.dir / "cwd")
        self.proj = self.dir / "proj"

    def tearDown(self):
        import shutil
        self.ss.CHAR_STATE_ROOT = self._root_orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_merge_puts_real_pictures_back(self):
        self.ss.save_chat_images(
            [{"data": self.b64, "media_type": "image/jpeg"}], 1000, "cass")
        hist = [_m("user", "[图片]", 1000), _m("user", "看这个", 1001),
                _m("assistant", "看到了", 1005)]
        out = chat_loop._merge_images(hist, "cass")
        self.assertEqual([m["text"] for m in out], ["看这个", "看到了"])
        self.assertEqual(len(out[0]["images"]), 1)
        self.assertNotIn("images", out[1])
        # 铸出来：一条 user 槽 = text + image
        import forge
        sid = forge.render(chat_loop._stamp_times(out), cwd=self.cwd,
                           projects_root=self.proj)
        evs = [json.loads(x) for x in forge.transcript_path(
            self.cwd, sid, self.proj).read_text("utf-8").splitlines()]
        self.assertEqual([b["type"] for b in evs[0]["message"]["content"]],
                         ["text", "image"])
        head = evs[0]["message"]["content"][0]["text"]
        self.assertTrue(head.startswith("【"))     # 戳落在合成后那条上
        self.assertIn("看这个", head)

    def test_three_images_one_caption(self):
        """09-01 20:52 的真实形状：三张图同一秒 + 一句话。ts 认不出是哪张，
        所以索引按 (ts, seq) 认领。"""
        self.ss.save_chat_images(
            [{"data": self.b64, "media_type": "image/jpeg"}] * 3, 1000, "cass")
        out = chat_loop._merge_images(
            [_m("user", "[图片]", 1000), _m("user", "[图片]", 1000),
             _m("user", "[图片]", 1000), _m("user", "daddydaddy 看这个", 1000)],
            "cass")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "daddydaddy 看这个")
        self.assertEqual(len(out[0]["images"]), 3)

    def test_keeps_placeholder_when_bytes_missing(self):
        """留底之前的老消息取不到字节 → 原样留占位符，不假装有图。"""
        self.ss.save_chat_images(
            [{"data": self.b64, "media_type": "image/jpeg"}], 1000, "cass")
        hist = [_m("user", "[图片]", 777), _m("user", "看这个", 778)]
        self.assertEqual(chat_loop._merge_images(hist, "cass"), hist)

    def test_lone_image_keeps_its_own_slot(self):
        """没配文字的图：自己一条，不去并后面隔了很久的那句话。"""
        self.ss.save_chat_images(
            [{"data": self.b64, "media_type": "image/jpeg"}], 1000, "cass")
        out = chat_loop._merge_images(
            [_m("user", "[图片]", 1000), _m("user", "过了半天才说的", 9999)], "cass")
        self.assertEqual(len(out), 2)
        self.assertEqual(len(out[0]["images"]), 1)
        self.assertNotIn("images", out[1])

    def test_save_is_idempotent(self):
        img = [{"data": self.b64, "media_type": "image/jpeg"}]
        self.ss.save_chat_images(img, 1000, "cass")
        self.ss.save_chat_images(img, 1000, "cass")     # 重试/补投不长第二笔账
        self.assertEqual(len(self.ss.read_chat_image_index("cass")), 1)

    def test_sha_path_traversal_rejected(self):
        self.assertIsNone(self.ss.read_chat_image("../../../etc/passwd", "cass"))
        self.assertIsNone(self.ss.read_chat_image("nothex", "cass"))


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

    def test_stamp_user_turns(self):
        """TA 的话逐条带时间锚——历史无时间戳曾让模型把 3 分钟前说的话说成
        「昨天」（2026-09-02）。他自己的话不加框（信感）。"""
        hist = [_m("user", "早", 1000), _m("assistant", "早，小狗", 1100)]
        out = chat_loop._stamp_times(hist)
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0]["text"].startswith("【01-01 周四 08:16】\n眠眠：早"))
        self.assertEqual(out[1]["text"], "早，小狗")        # assistant 原样
        self.assertEqual(hist[0]["text"], "早")             # 输入不被改写
        self.assertEqual(out, chat_loop._stamp_times(hist))  # 确定性

    def test_self_open_anchor_between_far_apart_assistants(self):
        """连着几次醒来 → 镜像里是连续 assistant，注入蒸发了。隔得够久就补一行
        时间锚回去，顺带把 forge 的「连续同角色合并」在这儿切开。"""
        gap = chat_loop.SELF_OPEN_GAP_SEC
        hist = [_m("user", "睡了", 1000),
                _m("assistant", "晚安", 1010),          # 回 TA 的话，紧跟着
                _m("assistant", "一点二十。你没声音了", 1010 + gap),   # 醒来
                _m("assistant", "（同一次开口的第二条）", 1010 + gap + 5),
                _m("assistant", "醒了没", 1010 + 3 * gap)]            # 又一次醒来
        out = chat_loop._stamp_times(hist)
        roles = [m["role"] for m in out]
        # 补了两行：两次"自己开的口"各一行；连发那条（+5 秒）不补
        self.assertEqual(roles, ["user", "assistant", "user", "assistant",
                                 "assistant", "user", "assistant"])
        self.assertRegex(out[2]["text"], r"^【\d\d-\d\d 周. \d\d:\d\d】$")
        self.assertEqual(out[2]["ts"], 1010 + gap)
        self.assertEqual(out[4]["text"], "（同一次开口的第二条）")  # 连发不切
        self.assertEqual(out, chat_loop._stamp_times(hist))          # 确定性

    def test_self_open_anchor_wake_wording(self):
        """锚点 ts 精确对上 wake_log（action=message）→ 还原活抬头同一句
        （wake_headline），**无条件**——短间隔（<600s）、前一条是 user 都补
        （确证醒来不吃连发启发式：最小醒来间隔 180s，10min 内完全可能）；
        对不上的槽（游戏 tick / 老存货）照旧只在隔够久时补光秃戳。"""
        import state_store
        gap = chat_loop.SELF_OPEN_GAP_SEC
        wts = 1010 + gap
        wts2 = wts + 300                # 第二次醒来：只隔 5 分钟（< 600s）
        orig = state_store.read_wake_log
        state_store.read_wake_log = lambda limit=None, char_id=None: [
            {"ts": wts, "action": "message", "trigger": "scheduled"},
            {"ts": wts2, "action": "message", "trigger": "mail"},
            {"ts": 999, "action": "none", "trigger": "auto"}]   # 安静醒着不参与
        try:
            hist = [_m("user", "睡了", 1000),
                    _m("assistant", "晚安", 1010),
                    _m("assistant", "到点了。", wts),                  # 定点醒来
                    _m("assistant", "有信。", wts2),                   # 10min 内硬触发
                    _m("assistant", "醒了没", wts2 + 3 * gap)]         # tick/存货
            out = chat_loop._stamp_times(hist, "cass")
            self.assertIn("这个点是你自己钉下要醒的", out[2]["text"])
            self.assertIn("外面有动静", out[4]["text"])               # 短间隔也补
            self.assertRegex(out[6]["text"], r"^【[^】]+】$")          # 光秃戳
            self.assertEqual(out, chat_loop._stamp_times(hist, "cass"))  # 确定性
            # 不传 char_id（单测纪律：不读生产 wake_log）＝全走光秃戳路
            bare = chat_loop._stamp_times(hist)
            self.assertRegex(bare[2]["text"], r"^【[^】]+】$")
        finally:
            state_store.read_wake_log = orig

    def test_self_open_not_added_after_user(self):
        """前一条是 TA 说的话 → 再慢也是"在回她"，不能标成"自己开的口"。"""
        hist = [_m("user", "在吗", 1000),
                _m("assistant", "在", 1000 + 5 * chat_loop.SELF_OPEN_GAP_SEC)]
        out = chat_loop._stamp_times(hist)
        self.assertEqual([m["role"] for m in out], ["user", "assistant"])

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

    # test_foldable_max_end 09-01 随「有可折活动段」那条压力判据一起删。


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

    async def test_tick_reports_the_clock_every_n(self):
        """「·」不带时间，一场几十上百轮下来他手上唯一的钟停在开场那句。每 N 轮
        报一次时（2026-09-02）——其余轮照旧光秃秃一个点，别浪费。"""
        orig = chat_loop.GAME_TICK_STAMP_EVERY
        chat_loop.GAME_TICK_STAMP_EVERY = 1      # 每轮都报，第一口就能看见
        try:
            self.assertTrue(await self._wait(
                lambda: self.clients and any(q.startswith("·\n【")
                                             for q in self.clients[-1].queries)))
            q = next(q for q in self.clients[-1].queries if q.startswith("·"))
            self.assertTrue(q.rstrip().endswith("】"))
            self.assertIn("周", q)               # 统一格式：MM-dd 周X HH:mm
        finally:
            chat_loop.GAME_TICK_STAMP_EVERY = orig
        # 默认档（10）下第一口是光秃秃的点：见 test_tick_reopens_delivers_and_ledgers

    async def test_tick_reopens_delivers_and_ledgers(self):
        """队列空 → 补「·」；session 没开先从镜像重起；点评 scrub 后投递+入账+
        事件账；tick 注入不进账不进铸造材料。"""
        self.assertTrue(await self._wait(
            lambda: self.clients and "·" in self.clients[-1].queries))
        c = self.clients[-1]
        # 镜像铸造：材料只有权威两条，没有「·」
        self.assertEqual([m["text"] for m in self.forged[0]],
                         [_stamp("去读两章"), "好，我去拿游戏"])
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

    async def test_pump_note_injection(self):
        """08-31 实锤修：app 游戏态聊天框走 /code/send → PumpNote 轮尾吃掉、
        回复走 outbox、账里逐字记 app 原文（注入包装不进账）。"""
        self.assertTrue(await self._wait(
            lambda: self.clients and "·" in self.clients[-1].queries))
        c = self.clients[-1]
        chat_loop.inject_pump_user("cass", "〔现在是 11:34〕\n别用五角星",
                                   "别用五角星")
        c.feed(self._Asst(content=[self._Text(text="好，改。")], model="t"),
               _result("x"))   # 在飞 tick 或 note 轮谁先到都由这发收掉
        c.feed(self._Asst(content=[self._Text(text="收到！")], model="t"),
               _result("x"))
        self.assertTrue(await self._wait(
            lambda: any(isinstance(q, str) and "别用五角星" in q
                        for q in c.queries)))
        self.assertTrue(await self._wait(lambda: len(self.delivered) >= 1))
        led = self.handle.meta["ledger"]
        self.assertTrue(await self._wait(
            lambda: any(e["r"] == "user"
                        and e["h"] == chat_loop._h("别用五角星")
                        for e in self.handle.meta["ledger"])))
        # 注入包装（时间头）没进账
        self.assertFalse(any(e["h"] == chat_loop._h("〔现在是 11:34〕\n别用五角星")
                             for e in led))
        self.assertTrue(self.handle.meta.get("game_pump"))

    async def test_pump_note_after_close_is_dropped_loudly(self):
        import notify
        barks: list[str] = []
        _orig = notify.bark_push
        notify.bark_push = lambda *a, **kw: barks.append(a[0] if a else "") or True
        try:
            self.assertTrue(await self._wait(
                lambda: self.clients and "·" in self.clients[-1].queries))
            self.handle.meta["end_requested"] = True
            self.clients[-1].feed(_result("x"))
            self.assertTrue(await self._wait(
                lambda: self.handle.meta.get("game_pump") is None))
            self.handle.queue.put_nowait(
                chat_loop.PumpNote(text="迟到的话", ledger_text="迟到的话"))
            self.assertTrue(await self._wait(lambda: barks))
            self.assertNotIn(chat_loop._h("迟到的话"),
                             [e["h"] for e in self.handle.meta["ledger"]])
        finally:
            notify.bark_push = _orig

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


class DisciplineBlockTest(unittest.TestCase):
    """PLAN_native §3：干活纪律常驻系统提示（不再按场注入——场没了）。
    capsule 收场约定是代码侧机制字段，机主文件缺席也在；占位走 persona 同款。"""

    def test_capsule_contract_always_present(self):
        import config
        orig = os.environ.get("CODE_ADDENDUM_CHAT_FILE")
        os.environ["CODE_ADDENDUM_CHAT_FILE"] = "no_such_file_xyz.md"
        try:
            blk = chat_loop._discipline_block("cass")
            self.assertIn("◆", blk)
            self.assertNotIn("上机", blk)               # 措辞纪律：能力不是场所
            self.assertNotIn("切过来", blk)             # 老路场文本不许混进统一路
        finally:
            if orig is None:
                os.environ.pop("CODE_ADDENDUM_CHAT_FILE", None)
            else:
                os.environ["CODE_ADDENDUM_CHAT_FILE"] = orig

    def test_owner_file_rendered_with_placeholders(self):
        import tempfile
        import config
        with tempfile.NamedTemporaryFile(
                "w", suffix=".md", dir=config.BASE_DIR, delete=False,
                encoding="utf-8") as f:
            f.write("提交只 add 自己动过的文件。{{USER_NAME}}的仓别用 -A。")
            name = Path(f.name).name
        orig = os.environ.get("CODE_ADDENDUM_CHAT_FILE")
        os.environ["CODE_ADDENDUM_CHAT_FILE"] = name
        try:
            blk = chat_loop._discipline_block("cass")
            self.assertIn("动手改东西时的纪律", blk)
            self.assertIn("只 add 自己动过的文件", blk)
            self.assertNotIn("{{USER_NAME}}", blk)      # 占位渲染掉了
            self.assertIn("◆", blk)                     # 机制字段仍在尾巴
        finally:
            (config.BASE_DIR / name).unlink(missing_ok=True)
            if orig is None:
                os.environ.pop("CODE_ADDENDUM_CHAT_FILE", None)
            else:
                os.environ["CODE_ADDENDUM_CHAT_FILE"] = orig


class TraceVocabTest(unittest.TestCase):
    """S3 补线①：留痕判线词表（别再借 NON_MEMORY_TOOLS——那管灰字过滤）。"""

    def test_external_stored(self):
        import pipeline
        for t in ("mail", "mail_draft", "browse", "webpage", "codemode",
                  "gamemode", "gametask"):
            self.assertTrue(pipeline.external_stored(t), t)
        for t in ("hold", "feel", "grow", "trace", "i", "", None):
            self.assertFalse(pipeline.external_stored(t), t)

    def test_external_tool(self):
        import pipeline
        for n in ("Edit", "Bash", "Read", "mcp__cassette-mail__mail_send",
                  "mcp__galatea-garden__create_reply"):
            self.assertTrue(pipeline.external_tool(n), n)
        for n in ("mcp__game__game_tap", "mcp__ombre-brain__hold",
                  "mcp__skills__skill_read", "ToolSearch", ""):
            self.assertFalse(pipeline.external_tool(n), n)


class ReadonlyGuardTest(unittest.TestCase):
    """PR14-b：只读常驻的路径闸（限根目录+黑名单，§5.3 安全面）。"""

    def test_paths(self):
        import pipeline
        import state_store
        g = pipeline.readonly_path_guard
        root = pipeline.CODE_ROOT
        # 放行：仓内文件 / 没点名路径（Grep 全局搜落恒空 cwd）/ 自己的房间
        self.assertIsNone(g({"file_path": str(root / "server" / "pipeline.py")},
                            "cass"))
        self.assertIsNone(g({}, "cass"))
        self.assertIsNone(g(None, "cass"))
        mine = state_store.CHAR_STATE_ROOT / "cass" / "wake_log.jsonl"
        self.assertIsNone(g({"file_path": str(mine)}, "cass"))
        # 放行：Claude Code 项目记忆目录（09-03 加白，「主仓 PLAN + memory 索引」老规矩）
        self.assertIsNone(g({"file_path": str(pipeline.MEMORY_ROOT / "MEMORY.md")},
                            "cass"))
        # 放行：dossier 整仓（09-05 加白），两个角色都看得到；.env* 黑名单照样生效
        for cid in ("cass", "default"):
            self.assertIsNone(g({"path": str(pipeline.DOSSIER_ROOT)}, cid))
            self.assertIsNone(
                g({"file_path": str(pipeline.DOSSIER_ROOT / "research" / "x.md")},
                  cid))
        self.assertIsNotNone(
            g({"file_path": str(pipeline.DOSSIER_ROOT / ".env.local")}, "cass"))
        # 拒：dossier 的邻居（家目录别的仓不因为 dossier 放行而连带放行）
        self.assertIsNotNone(
            g({"file_path": str(pipeline.DOSSIER_ROOT.parent / "dossier2" / "a.md")},
              "cass"))
        # 拒：根目录外 / .env* / 私人仓 / 别的角色的房间
        self.assertIsNotNone(g({"file_path": "/etc/passwd"}, "cass"))
        # 拒：memory 之外的 ~/.claude（settings/凭据/别的项目的记忆都不放）
        self.assertIsNotNone(
            g({"file_path": str(pipeline.MEMORY_ROOT.parents[2] / "settings.json")},
              "cass"))
        self.assertIsNotNone(
            g({"file_path": str(pipeline.MEMORY_ROOT.parent / "sessions" / "x.jsonl")},
              "cass"))
        self.assertIsNotNone(g({"file_path": str(root / ".env")}, "cass"))
        self.assertIsNotNone(g({"file_path": str(root / "server" / ".env.local")},
                               "cass"))
        self.assertIsNotNone(g({"path": "/Users/nemu/mianmian-app/x.py"}, "cass"))
        other = state_store.CHAR_STATE_ROOT / "default" / "wake_log.jsonl"
        self.assertIsNotNone(g({"file_path": str(other)}, "cass"))
        # 相对路径必须解析后再查：「../」一步就从恒空 cwd 爬进 state/characters
        self.assertIsNotNone(
            g({"file_path": "../characters/default/char.json"}, "cass"))


class ReadonlyGateTest(unittest.IsolatedAsyncioTestCase):
    """PR14-b：门里只读工具先于醒来禁用面（任何轮来源都能看，但过路径闸）。"""

    async def test_readonly_bypasses_wake_denylist_but_not_guard(self):
        import pipeline
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["turn_kind"] = "wake"
        handle.meta["wake_tools"] = set()          # 醒来挂载表里没有它们
        gate = chat_loop._wake_gate(handle)
        ok = await gate({"tool_name": "Read",
                         "tool_input": {"file_path":
                                        str(pipeline.CODE_ROOT / "README.md")}},
                        "t1", None)
        self.assertEqual(ok, {})
        bad = await gate({"tool_name": "Read",
                          "tool_input": {"file_path": "/etc/passwd"}}, "t2", None)
        self.assertEqual(
            bad["hookSpecificOutput"]["permissionDecision"], "deny")


class _AlTmpBase(unittest.TestCase):
    """activity_log 全目录打到临时地（S3 留痕用例：事件账/行为账都要落）。"""

    def setUp(self):
        import tempfile
        import activity_log
        self.al = activity_log
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = (activity_log.PATH, activity_log.ACT_DIR,
                      activity_log.SEG_DIR, activity_log.SHOT_DIR,
                      activity_log.OPEN_PATH)
        activity_log.PATH = root / "activity_log.jsonl"
        activity_log.ACT_DIR = root / "activity"
        activity_log.SEG_DIR = activity_log.ACT_DIR / "segments"
        activity_log.SHOT_DIR = activity_log.ACT_DIR / "shots"
        activity_log.OPEN_PATH = activity_log.ACT_DIR / "open_segments.json"

    def tearDown(self):
        (self.al.PATH, self.al.ACT_DIR, self.al.SEG_DIR, self.al.SHOT_DIR,
         self.al.OPEN_PATH) = self._orig
        self.tmp.cleanup()


class ToolTraceTest(_AlTmpBase):
    """S3 补线②：执行层 tool 落账——配对成事件、地址=开着的段、判线字段齐。"""

    def test_pairs_land_in_open_segment(self):
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["game_pump"] = {"seg_id": "cass-game-1000"}
        handle.meta["turn_kind"] = "wake"
        tr = chat_loop._ToolTrace(handle)
        tr.use("t1", "Bash", {"command": "git status"})
        tr.result("t1", False)
        tr.use("t2", "mcp__game__game_tap", {"x": 1})
        tr.result("t2", False)
        tr.use("t3", "Read", {"file_path": "server/x.py"})
        tr.result("t3", True)
        evs = self.al.read_events("cass-game-1000")
        self.assertEqual([e["name"] for e in evs],
                         ["Bash", "mcp__game__game_tap", "Read"])
        self.assertEqual(evs[0]["text"], "git status")
        self.assertTrue(evs[0]["ext"])
        self.assertFalse(evs[0]["ro"])
        self.assertTrue(evs[0]["ok"])
        self.assertEqual(evs[0]["turn"], "wake")
        self.assertFalse(evs[1]["ext"])          # game 点按=第四类，读取侧不上摘要
        self.assertTrue(evs[2]["ro"])
        self.assertFalse(evs[2]["ok"])           # is_error=True → ok False

    def test_no_segment_no_event(self):
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        tr = chat_loop._ToolTrace(handle)
        tr.use("t1", "Bash", {"command": "ls"})
        tr.result("t1", False)
        self.assertEqual(self.al.read_events("cass-game-1000"), [])

    def test_unpaired_result_ignored(self):
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["game_pump"] = {"seg_id": "cass-game-1000"}
        tr = chat_loop._ToolTrace(handle)
        tr.result("never-seen", False)
        self.assertEqual(self.al.read_events("cass-game-1000"), [])
        self.assertEqual(self.al.recent_acts("cass"), [])

    def test_receipt_lands_and_readonly_skips_it(self):
        """回执列（§7.2）：写类存原话、只读工具不存（返回是内容不是回执）。"""
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["game_pump"] = {"seg_id": "cass-game-1000"}
        tr = chat_loop._ToolTrace(handle)
        tr.use("t1", "Bash", {"command": "pytest -q"})
        tr.result("t1", False, "5 failed, 430 passed")
        tr.use("t2", "Read", {"file_path": "server/x.py"})
        tr.result("t2", False, "整个文件的内容" * 100)
        evs = self.al.read_events("cass-game-1000")
        self.assertEqual(evs[0]["ret"], "5 failed, 430 passed")
        self.assertEqual(evs[1]["ret"], "")          # 只读不存回执

    def test_receipt_accepts_all_three_content_shapes(self):
        """`ToolResultBlock.content` 是 str | list[dict] | None 三形。"""
        self.assertEqual(chat_loop._result_text("寄到了。"), "寄到了。")
        self.assertEqual(
            chat_loop._result_text([{"type": "text", "text": "寄到了。"},
                                    {"type": "image", "source": {}},
                                    {"type": "text", "text": "没留副本。"}]),
            "寄到了。\n没留副本。")
        self.assertEqual(chat_loop._result_text(None), "")
        self.assertEqual(chat_loop._result_text(object()), "")

    def test_every_call_site_passes_content(self):
        """09-01 复盘第 4 步扫出来的：`_ToolTrace` 有**两个**调用点——常规轮
        (`_turn_events`) 和 game 泵轮——头一版补线只改了前一个，泵轮的回执
        整条丢掉。补线类的坑一贯长这样：**改的是函数，漏的是第二个调用点。**
        这条守着「以后再加第三个调用点也不许忘」。"""
        import inspect
        import re
        src = inspect.getsource(chat_loop)
        calls = re.findall(r"trace\.result\((.*?)\)", src, re.S)
        self.assertGreaterEqual(len(calls), 2)
        for c in calls:
            self.assertEqual(len(c.split(",")), 3, f"少传 content: {c!r}")

    def test_ok_eats_structural_refusal_not_wording(self):
        """`ok` 的口径（眠眠 09-01 12:16 拍板）：吃 ①② 结构判据，不吃 ③ 文本表。

        ①`is_error` 和 ②`{"ok": false}` 是专门表示成败的字段，读到什么是什么；
        ③ 婉拒名单给的是「像失败」不是「是失败」，进账本会被下一轮当既成事实。"""
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["game_pump"] = {"seg_id": "cass-game-1000"}
        tr = chat_loop._ToolTrace(handle)
        # ②：协议层没报错，返回体自己说没成
        tr.use("t1", "Bash", {"command": "x"})
        tr.result("t1", False, '{"ok": false, "error": "沙盒里没这个命令"}')
        # ③：命中婉拒名单，但账本不认（tool_result_error 那条线才认）
        tr.use("t2", "Bash", {"command": "y"})
        tr.result("t2", False, "错误：未找到记忆桶")
        # 业务层软拒绝：两道结构判据都不命中，ok 照样 True，真相在 ret 里
        tr.use("t3", "mcp__beacon__write_letter", {"subject": "补一封"})
        tr.result("t3", False, "今天已经寄了 3 封了。明天再来。")
        evs = self.al.read_events("cass-game-1000")
        self.assertFalse(evs[0]["ok"])                      # ② 认
        self.assertTrue(evs[1]["ok"])                       # ③ 不认
        self.assertTrue(evs[2]["ok"])                       # 结构判据兜不住
        self.assertEqual(evs[2]["ret"], "今天已经寄了 3 封了。明天再来。")

    def test_structural_judgement_layering(self):
        """①② 抽出来给留痕侧用，`tool_result_error` 复用它、外加 ③（行为不变）。"""
        import pipeline
        f = pipeline.tool_result_error_structural
        self.assertTrue(f(True, "boom"))                    # ①
        self.assertTrue(f(False, '{"ok": false, "error": "没成"}'))   # ②
        self.assertIsNone(f(False, '{"ok": true}'))
        self.assertIsNone(f(False, "错误：未找到记忆桶"))    # ③ 不在这一层
        self.assertIsNone(f(False, "今天已经寄了 3 封了。明天再来。"))
        # 老口径那条线三道都还在
        self.assertTrue(pipeline.tool_result_error(
            {"content": "错误：未找到记忆桶"}))
        self.assertTrue(pipeline.tool_result_error(
            {"content": '{"ok": false, "error": "没成"}'}))
        self.assertIsNone(pipeline.tool_result_error({"content": "寄到了。"}))

    def test_receipt_capped(self):
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["game_pump"] = {"seg_id": "cass-game-1000"}
        tr = chat_loop._ToolTrace(handle)
        tr.use("t1", "Bash", {"command": "cat big"})
        tr.result("t1", False, "x" * 5000)
        self.assertEqual(len(self.al.read_events("cass-game-1000")[0]["ret"]),
                         chat_loop.RET_CAP)


class PronounHintTest(unittest.TestCase):
    """人称提示两个轴：代词性别常在；第二人称视角聊天/醒来要、同居世界不要。"""

    def test_second_person_on_by_default(self):
        import pipeline
        h = pipeline.pronoun_hint()
        self.assertIn("人称代词一律用", h)
        self.assertIn("都用「你」", h)
        self.assertIn("〔〕包起来的心里话", h)

    def test_cohabit_keeps_pronoun_only(self):
        import pipeline
        h = pipeline.pronoun_hint(second_person=False)
        self.assertIn("人称代词一律用", h)
        self.assertNotIn("都用「你」", h)
        self.assertTrue(h.endswith("】"))

    def test_cohabit_path_opts_out(self):
        import inspect
        import cohabit
        self.assertIn("pronoun_hint(second_person=False)",
                      inspect.getsource(cohabit))


class CtxFromUsageTest(unittest.IsolatedAsyncioTestCase):
    """08-31：窗口压力量真实上下文，不再拿 estimate_tokens 数对话文本
    （旧口径漏系统提示/工具 schema/工具入参返回/thinking，实测偏小 4.4 倍）。"""

    def test_sums_the_whole_prompt_plus_output(self):
        # 事故那轮的真实 usage（21:24:42）
        self.assertEqual(chat_loop._ctx_from_usage(
            {"input_tokens": 2, "cache_creation_input_tokens": 2506,
             "cache_read_input_tokens": 97633, "output_tokens": 877}), 101018)

    def test_missing_fields_and_none(self):
        self.assertEqual(chat_loop._ctx_from_usage(None), 0)
        self.assertEqual(chat_loop._ctx_from_usage({"output_tokens": 5}), 5)

    async def test_tracks_last_request_not_the_sum(self):
        """多请求的轮：取最后一条 AssistantMessage（＝当前 prompt 的真身），
        绝不累加——ResultMessage.usage 是整轮求和，加起来会虚高好几倍。"""
        from claude_agent_sdk.types import AssistantMessage
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        client = FakeClient(options=None)
        client.feed(
            AssistantMessage(content=[], model="m",
                             usage={"cache_read_input_tokens": 90000,
                                    "cache_creation_input_tokens": 800,
                                    "output_tokens": 60}),
            AssistantMessage(content=[], model="m",
                             usage={"cache_read_input_tokens": 90860,
                                    "cache_creation_input_tokens": 1500,
                                    "output_tokens": 200}),
            _result("好了"))
        async for _ in chat_loop._turn_events(client, handle, {}):
            pass
        self.assertEqual(handle.meta["ctx_est"], 92560)   # 末条，不是 183,420


class ActsFromExecutionTest(_AlTmpBase):
    """08-31 事故修：行为账从执行层落，不再从 stored 镜像。事故=聊天轮连着
    两轮说「信发出去了」「记忆存了」，实际一次工具都没调；而聊天轮不开段、
    行为账当时唯一的写入点在醒来轮，空白不构成反证。"""

    def _trace(self, kind="chat"):
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["turn_kind"] = kind
        return chat_loop._ToolTrace(handle)

    def test_chat_turn_lands_without_segment(self):
        """聊天轮没有开着的段——事件账落不下，行为账照落。"""
        tr = self._trace()
        tr.use("t1", "mcp__beacon__write_letter",
               {"to": "24c8ed", "subject": "那段距离", "body": "长正文" * 50})
        tr.result("t1", False)
        acts = self.al.recent_acts("cass")
        self.assertEqual([a["tool"] for a in acts], ["mcp__beacon__write_letter"])
        self.assertEqual(acts[0]["text"], "那段距离")   # subject 优先于 body
        self.assertEqual(acts[0]["scene"], "chat")
        self.assertTrue(acts[0]["ok"])

    def test_failed_call_recorded_not_ok(self):
        tr = self._trace()
        tr.use("t1", "mcp__browser__browser_navigate", {"url": "https://x.dev"})
        tr.result("t1", True)
        act = self.al.recent_acts("cass")[0]
        self.assertEqual(act["text"], "https://x.dev")
        self.assertFalse(act["ok"])

    def test_internal_and_readonly_stay_out_writes_land(self):
        """判线 acts_worthy（PLAN_native §4）：Ombre/游戏点按=内部不落、只读
        不落（翻文件是看不是做）；写类每条都是账上一行——一行自带机主批准
        这个事实（没批的根本执行不到）。"""
        tr = self._trace()
        for i, (name, inp) in enumerate([
                ("mcp__ombre-brain__hold", {"title": "一条记忆"}),
                ("mcp__game__game_tap", {"x": 1}),
                ("mcp__skills__skill_read", {"name": "jobhunt"}),
                ("Read", {"file_path": "server/app.py"})]):
            tr.use(f"t{i}", name, inp)
            tr.result(f"t{i}", False)
        self.assertEqual(self.al.recent_acts("cass"), [])
        tr.use("w1", "Bash", {"command": "pytest"})
        tr.result("w1", False)
        tr.use("w2", "Edit", {"file_path": "server/x.py"})
        tr.result("w2", False)
        acts = self.al.recent_acts("cass")
        self.assertEqual([a["tool"] for a in acts], ["Bash", "Edit"])
        self.assertEqual(acts[0]["text"], "pytest")

    def test_wake_turn_same_ledger(self):
        """两种轮一个口径（醒来轮也走常驻 session，不再靠 wake_sdk 镜像）。"""
        tr = self._trace(kind="wake")
        tr.use("t1", "mcp__mail__mail_read", {"uid": "1786430786"})
        tr.result("t1", False)
        act = self.al.recent_acts("cass")[0]
        self.assertEqual(act["scene"], "wake")
        self.assertEqual(act["text"], "1786430786")

    def test_business_refusal_keeps_ok_but_keeps_receipt(self):
        """09-01 11:10 回归样本：一封信被配额挡回来，一个字没寄出去。

        业务层的拒绝在 `is_error` 上跟成功同形——所以 `ok` 照样 True（不猜、
        不扫词改判），但回执原话留在 `ret` 里。**账从「只能答调没调」变成
        「还能答成没成」**，两列各答一个问题。"""
        tr = self._trace()
        tr.use("t1", "mcp__beacon__write_letter",
               {"to": "24c8ed", "subject": "你有两封信寄错到我这儿了"})
        tr.result("t1", False, "今天已经寄了 3 封了。明天再来。")
        act = self.al.recent_acts("cass")[0]
        self.assertTrue(act["ok"])                   # 协议层确实没报错
        self.assertEqual(act["ret"], "今天已经寄了 3 封了。明天再来。")

    def test_real_send_receipt(self):
        """对照组：同一个工具真寄出去了，回执长得不一样。"""
        tr = self._trace()
        tr.use("t1", "mcp__beacon__write_letter", {"subject": "那段距离"})
        tr.result("t1", False, "寄到了。内容没有留下任何副本。")
        act = self.al.recent_acts("cass")[0]
        self.assertTrue(act["ok"])
        self.assertEqual(act["ret"], "寄到了。内容没有留下任何副本。")



class FoldTraceTest(_AlTmpBase):
    """S3 补线③：折叠段从事件账 derive 经历摘要（写类逐条、读类聚合、
    第四类不上、没账回光框）。"""

    HIST = [_m("user", "去读吧", 1400),
            _m("assistant", "这句台词妙", 1600),
            _m("assistant", "第二场的点评", 3200),
            _m("user", "读完啦？", 3600)]

    def _two_intervals(self):
        self.al.append_interval("cass", "game", 1500, 1800, note="《如鸢》剧情")
        self.al.append_interval("cass", "game", 3000, 3500, note="《如鸢》剧情")

    def test_folded_frame_carries_trace(self):
        self._two_intervals()
        seg = self.al.interval_seg_id("cass", "game", 1500)
        self.al.append_event(seg, "tool", name="Edit", text="server/x.py",
                             ok=True, ext=True, ro=False, turn="chat")
        self.al.append_event(seg, "tool", name="Bash", text="pytest -q",
                             ok=False, ext=True, ro=False, turn="chat")
        self.al.append_event(seg, "tool", name="Read", text="server/y.py",
                             ok=True, ext=True, ro=True, turn="chat")
        self.al.append_event(seg, "tool", name="Read", text="server/y.py",
                             ok=True, ext=True, ro=True, turn="chat")
        self.al.append_event(seg, "tool", name="mcp__game__game_tap", text="{}",
                             ok=True, ext=False, ro=False, turn="wake")
        self.al.append_event(seg, "comment", text="一条点评")
        out = chat_loop._frame_activities(self.HIST, "cass")
        folded = out[1]["text"]
        self.assertIn("读了一场", folded)                 # 原光框还在
        self.assertIn("亲手做过的", folded)
        self.assertIn("Edit：server/x.py", folded)
        self.assertIn("Bash：pytest -q（没成）", folded)
        self.assertIn("翻看过：server/y.py", folded)      # 读类聚合+去重
        self.assertEqual(folded.count("server/y.py"), 1)
        self.assertNotIn("game_tap", folded)              # 第四类不上摘要
        self.assertNotIn("一条点评", folded)              # 点评不进摘要（气泡自有）
        # 最近一场照旧两行框，不带摘要
        self.assertIn("你开了《如鸢》剧情会话", out[2]["text"])
        self.assertNotIn("亲手", out[2]["text"])
        # 确定性：同输入同字节
        self.assertEqual(out, chat_loop._frame_activities(self.HIST, "cass"))

    def test_folded_frame_shows_receipt(self):
        """写类那行带回执（§7.2 c）：ok=True 不等于办成了，原话才算数。
        老账行没有 ret（09-01 之前落的）→ 缺键取空，那行与从前一字不差。"""
        self._two_intervals()
        seg = self.al.interval_seg_id("cass", "game", 1500)
        self.al.append_event(seg, "tool", name="mcp__beacon__write_letter",
                             text="你有两封信寄错到我这儿了", ok=True,
                             ret="今天已经寄了 3 封了。明天再来。",
                             ext=True, ro=False, turn="chat")
        self.al.append_event(seg, "tool", name="Edit", text="server/x.py",
                             ok=True, ext=True, ro=False, turn="chat")
        folded = chat_loop._frame_activities(self.HIST, "cass")[1]["text"]
        self.assertIn("write_letter：你有两封信寄错到我这儿了"
                      " → 今天已经寄了 3 封了。明天再来。", folded)
        self.assertIn("Edit：server/x.py", folded)    # 没 ret 的行原样
        self.assertNotIn("Edit：server/x.py →", folded)

    def test_no_events_plain_frame(self):
        """事件账空（或过了保留窗被清）→ 折叠框与从前一字不差。"""
        self._two_intervals()
        out = chat_loop._frame_activities(self.HIST, "cass")
        folded = out[1]["text"]
        self.assertIn("读了一场", folded)
        self.assertNotIn("亲手", folded)

    def test_capsule_sorts_before_actions(self):
        """PR14-d：capsule（第二类留痕）排在动作清单最前——结论先于动作。
        （原来这条同时钉 code 场的折叠措辞；code 起止 2026-09-02 退役，
        只剩 game 一种场，措辞那半跟着删了。）"""
        self._two_intervals()
        seg = self.al.interval_seg_id("cass", "game", 1500)
        self.al.append_event(seg, "capsule", text="工具不过桥 ← forge.py:241")
        self.al.append_event(seg, "tool", name="Edit", text="server/x.py",
                             ok=True, ext=True, ro=False, turn="chat")
        out = chat_loop._frame_activities(self.HIST, "cass")
        folded = out[1]["text"]
        self.assertIn("◆ 工具不过桥 ← forge.py:241", folded)
        self.assertIn("Edit：server/x.py", folded)
        idx_cap = folded.index("◆ 工具不过桥")
        idx_act = folded.index("Edit：server/x.py")
        self.assertLess(idx_cap, idx_act)               # 结论先于动作清单

    def test_act_cap_with_overflow_line(self):
        self._two_intervals()
        seg = self.al.interval_seg_id("cass", "game", 1500)
        for i in range(15):
            self.al.append_event(seg, "tool", name="Bash", text=f"cmd-{i}",
                                 ok=True, ext=True, ro=False, turn="chat")
        folded = chat_loop._frame_activities(self.HIST, "cass")[1]["text"]
        self.assertIn("cmd-11", folded)
        self.assertNotIn("cmd-12", folded)
        self.assertIn("还有 3 件", folded)


if __name__ == "__main__":
    unittest.main()
