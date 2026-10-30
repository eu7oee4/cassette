"""PLAN_interject P2（§9 第 10 步）：轮内插话的引擎侧。

路由（stream_turn 在飞时写进 session / 否则排队）、hook 吸收判定（prompt_id 比对、FIFO 配对）、
原流 interject_point + 插话流 done{merged}、没被吸收时轮尾续读一轮、账按气泡交错、死亡兜底、
弱检查退回排队、hook 不来自动关闸。引擎用假 client（脚本化消息流），hook 由测试直接调
（真 CLI 在工具边界触发，形状见 docs/probes/2026-10-29-step1/interject.md）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_interject -v
"""
import asyncio
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import session_mgr as sm
import sse
from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import (AssistantMessage, StreamEvent, ToolResultBlock,
                                    ToolUseBlock, UserMessage)

TOOL = "mcp__mail__mail_read"
import time
T = int(time.time())     # 整条链用例的时间基准：窗口里的假 ts 往后偏，账里他的气泡 ts（真实时刻 ∨ TA 那条）才和窗口同序


def _se(event):
    return StreamEvent(uuid="u", session_id="s", event=event)


def _text_events(text):
    return [_se({"type": "content_block_start", "content_block": {"type": "text"}}),
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})]


def _result(text, is_error=False):
    return ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0, is_error=is_error,
                         num_turns=1, session_id="s", result=None if is_error else text)


def _tool_pair(tid="t1"):
    return [AssistantMessage(content=[ToolUseBlock(id=tid, name=TOOL, input={})], model="m"),
            UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="没新信")])]


def _payloads(chunks: list[bytes]) -> list[dict]:
    return [json.loads(c.decode("utf-8")[len("data: "):].strip()) for c in chunks]


# ---------- 纯函数 ----------

class TurnEntriesTest(unittest.TestCase):
    def test_interject_lands_between_segments(self):
        import chat_loop
        tr = {"saw_break": True,
              "segments": [{"text": "先说一句。", "ts": 50},
                           {"interject": True, "rid": "i1", "text": "等等", "ts": 60},
                           {"text": "再说一句。", "ts": 70}]}
        self.assertEqual(chat_loop._turn_entries(tr, "x", 40),
                         [("assistant", "先说一句。", 50), ("user", "等等", 60),
                          ("assistant", "再说一句。", 70)])
        self.assertEqual(chat_loop._reply_bubbles(tr, "x", 40),
                         [("先说一句。", 50), ("再说一句。", 70)])

    def test_interject_before_any_text(self):
        import chat_loop
        tr = {"saw_break": True,
              "segments": [{"interject": True, "rid": "i1", "text": "等等", "ts": 60},
                           {"text": "来了。", "ts": 70}]}
        self.assertEqual(chat_loop._turn_entries(tr, "来了。", 100),
                         [("user", "等等", 100), ("assistant", "来了。", 100)])   # 不早于 TA 那条


class TranslateInterjectPointTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, events):
        tr: dict = {}
        out = []

        async def gen():
            for e in events:
                yield e
        async for c in sse.translate_events(gen(), lambda r, s: {"reply": r}, trace=tr):
            out.append(c)
        return _payloads(out), tr

    async def test_point_seals_bubble_and_marks_trace(self):
        evs = [{"type": "stream_event", "event": e.event} for e in _text_events("先说一句。")]
        evs.append({"type": "interject_point", "rid": "i1", "text": "等等", "ts": 500})
        evs += [{"type": "stream_event", "event": e.event} for e in _text_events("再说。")]
        evs.append({"type": "result", "result": "再说。", "is_error": False})
        pl, tr = await self._run(evs)
        types_ = [p["type"] for p in pl]
        self.assertEqual(types_, ["text", "text_break", "interject_point", "text", "done"])
        self.assertEqual(pl[2]["rid"], "i1")
        self.assertTrue(tr["saw_break"])
        self.assertEqual([s.get("text") for s in tr["segments"]], ["先说一句。", "等等", "再说。"])
        self.assertTrue(tr["segments"][1]["interject"])
        self.assertEqual(pl[-1]["reply"], "先说一句。\n\n再说。")

    async def test_point_with_no_text_yet_has_no_break(self):
        evs = [{"type": "interject_point", "rid": "i1", "text": "等等", "ts": 500}]
        evs += [{"type": "stream_event", "event": e.event} for e in _text_events("来了。")]
        evs.append({"type": "result", "result": "来了。", "is_error": False})
        pl, tr = await self._run(evs)
        self.assertEqual([p["type"] for p in pl], ["interject_point", "text", "done"])
        self.assertTrue(tr["saw_break"])


class FitTest(unittest.TestCase):
    def setUp(self):
        import chat_loop
        self.cl = chat_loop
        self.led = [{"r": "user", "h": chat_loop._h("a"), "ts": 1000},
                    {"r": "assistant", "h": chat_loop._h("b"), "ts": 1001}]
        self.h = chat_loop._h("c")

    def test_snapshot_is_ledger_tail_plus_inflight(self):
        hist = [{"role": "user", "text": "a", "ts": 1000}, {"role": "assistant", "text": "b", "ts": 1001},
                {"role": "user", "text": "c", "ts": 2000}, {"role": "assistant", "text": "段1", "ts": 2001}]
        self.assertTrue(self.cl._interject_fit(self.led, hist, self.h))
        # 账尾多一条 assistant（stale）也算
        self.assertTrue(self.cl._interject_fit(self.led, hist[:1] + hist[2:], self.h))

    def test_edited_or_missing(self):
        hist = [{"role": "user", "text": "a", "ts": 1000}, {"role": "assistant", "text": "改了", "ts": 1001},
                {"role": "user", "text": "c", "ts": 2000}]
        self.assertFalse(self.cl._interject_fit(self.led, hist, self.h))
        self.assertFalse(self.cl._interject_fit(self.led, hist[:2], self.h))      # 没有在飞那条


class HookTest(unittest.IsolatedAsyncioTestCase):
    async def test_pid_assignment_fifo(self):
        import chat_loop
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        hook = chat_loop._prompt_hook(handle)
        self.assertEqual(await hook({"prompt_id": "x"}, None, None), {})     # 没在飞：不记
        infl = {"rid": "r1", "pid": None, "text_h": "", "pending": [], "absorbed_q": []}
        handle.meta["inflight"] = infl
        await hook({"prompt_id": "p1"}, None, None)
        self.assertEqual(infl["pid"], "p1")
        a = chat_loop.Interject(rid="i1", text="x", ledger_text="x", ts=1, finalize=lambda r, s: {})
        b = chat_loop.Interject(rid="i2", text="x", ledger_text="x", ts=1, finalize=lambda r, s: {})
        infl["pending"] += [a, b]
        await hook({"prompt_id": "p1"}, None, None)      # 同串文字：按入队顺序配
        await hook({"prompt_id": "p9"}, None, None)
        self.assertEqual((a.absorbed, b.absorbed), (True, False))
        self.assertEqual(infl["absorbed_q"], [a])


# ---------- 整条链 ----------

class _Client:
    def __init__(self, options, owner):
        self.options = options
        self.owner = owner
        self.queries: list = []
        self.disconnected = False
        self._q: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        pass

    async def query(self, payload):
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


class InterjectLoopTest(unittest.IsolatedAsyncioTestCase):
    CHAR = "cass"

    async def asyncSetUp(self):
        import chat_loop
        import notify
        import activity_log
        import state_store
        self.cl = chat_loop
        sm.set_event_loop(asyncio.get_running_loop())
        sm._registry.clear()
        chat_loop.INTERJECT_OFF.clear()
        self.clients: list[_Client] = []
        self.forged: list[list[dict]] = []
        self.barks: list[str] = []
        self._saved = (config.INTERJECT, chat_loop.forge, chat_loop._note_usage, chat_loop._persist_ledger,
                       chat_loop._ombre_on, chat_loop._frame_activities, chat_loop._mount_signature,
                       chat_loop._cache_tool_tokens, notify.bark_push_bg, notify.bark_push,
                       activity_log.append_act, activity_log.append_event, state_store.touch_tools)
        config.INTERJECT = "cass"
        chat_loop.forge = types.SimpleNamespace(
            render=lambda msgs, **kw: (self.forged.append(list(msgs)), f"sid-{len(self.forged)}")[1])
        chat_loop._note_usage = lambda *a: None
        chat_loop._persist_ledger = lambda *a: None
        chat_loop._ombre_on = lambda cid: False
        chat_loop._frame_activities = lambda h, cid: h
        chat_loop._mount_signature = lambda cid: ((), True)

        async def _no_cache(c, h):
            return None
        chat_loop._cache_tool_tokens = _no_cache
        notify.bark_push_bg = lambda text, title=None: self.barks.append(text)
        notify.bark_push = lambda text, title=None: True
        activity_log.append_act = lambda *a, **k: None
        activity_log.append_event = lambda *a, **k: None
        state_store.touch_tools = lambda *a, **k: None
        self.handle = sm.LoopHandle(char_id=self.CHAR, scene="chat")
        sm._registry[(self.CHAR, "chat")] = self.handle
        self.hook = chat_loop._prompt_hook(self.handle)
        opts = lambda cid, catalog=None: SimpleNamespace(cwd="/tmp/nowhere", resume=None,
                                                         mcp_servers={}, tools=[TOOL])
        self.task = asyncio.create_task(chat_loop.run(
            self.handle, client_factory=lambda o: self._mk(o), options_factory=opts))
        self.handle.task = self.task
        await asyncio.sleep(0)

    def _mk(self, options):
        c = _Client(options, self)
        self.clients.append(c)
        return c

    async def asyncTearDown(self):
        import chat_loop
        import notify
        import activity_log
        import state_store
        if not self.task.done():
            self.handle.stop_reason = "test-teardown"
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        (config.INTERJECT, chat_loop.forge, chat_loop._note_usage, chat_loop._persist_ledger,
         chat_loop._ombre_on, chat_loop._frame_activities, chat_loop._mount_signature,
         chat_loop._cache_tool_tokens, notify.bark_push_bg, notify.bark_push,
         activity_log.append_act, activity_log.append_event, state_store.touch_tools) = self._saved
        chat_loop.INTERJECT_OFF.clear()
        sm._registry.clear()

    async def _wait(self, cond, n=400):
        for _ in range(n):
            await asyncio.sleep(0.005)
            if cond():
                return True
        return False

    def _stream(self, messages, rid, interject_ok=True, box=None):
        """起一个 /chat/stream 的消费者任务，把 SSE 载荷收进 box。"""
        box = box if box is not None else []

        async def consume():
            async for c in self.cl.stream_turn(
                    char_id=self.CHAR, rid=rid, messages=messages, injection=messages[-1]["text"],
                    finalize=lambda reply, stored: {"reply": reply, "stored": stored},
                    interject_ok=interject_ok):
                box.append(json.loads(c.decode("utf-8")[len("data: "):].strip()))
            box.append(None)
        return asyncio.create_task(consume()), box

    HIST = [{"role": "user", "text": "早", "ts": T + 0}, {"role": "assistant", "text": "早", "ts": T + 1}]

    async def _start_main(self, hist=None):
        """主轮：TA 说「看信箱」，他先说一句、去调工具。喂到工具返回为止，hook#1 已到。"""
        t_main, box = self._stream((hist or self.HIST) + [{"role": "user", "text": "看信箱", "ts": T + 1000}], "main")
        self.assertTrue(await self._wait(lambda: self.clients and self.clients[-1].queries))
        await self.hook({"prompt_id": "P-main"}, None, None)          # hook#1（query 时触发）
        c = self.clients[-1]
        c.feed(*_text_events("我看看。"), *_tool_pair("t1"))
        self.assertTrue(await self._wait(lambda: any(p and p.get("type") == "text" for p in box)))
        return t_main, box, c

    async def test_absorbed(self):
        t_main, main_box, c = await self._start_main()
        snap = self.HIST + [{"role": "user", "text": "看信箱", "ts": T + 1000},
                            {"role": "assistant", "text": "我看看。", "ts": T + 1001},
                            {"role": "user", "text": "顺便看下垃圾箱", "ts": T + 1005}]
        t_ij, ij_box = self._stream(snap, "ij1")
        self.assertTrue(await self._wait(lambda: len(c.queries) == 2))      # 写进了同一条 session
        sent = c.queries[1][0]["message"]["content"][0]["text"]
        self.assertTrue(sent.startswith("【") and sent.endswith("】\n顺便看下垃圾箱"))   # 时间头 + 原文
        self.assertEqual(ij_box[0], {"type": "interject", "into": "main"})
        # CLI 在工具边界吸收：hook#2 带同一个 prompt_id → 下一段文字前原流发 interject_point
        await self.hook({"prompt_id": "P-main", "prompt": sent}, None, None)
        c.feed(*_text_events("垃圾箱也空的。"), _result("垃圾箱也空的。"))
        await asyncio.wait_for(t_main, 3)
        await asyncio.wait_for(t_ij, 3)
        types_ = [p["type"] for p in main_box if p]
        self.assertEqual(types_, ["text", "text_break", "interject_point", "text", "done"])
        self.assertEqual([p for p in main_box if p and p["type"] == "interject_point"][0]["rid"], "ij1")
        self.assertEqual([p for p in ij_box if p], [{"type": "interject", "into": "main"},
                                                   {"type": "done", "merged": "main"}])
        led = self.handle.meta["ledger"]
        self.assertEqual([(e["r"], e["h"]) for e in led[2:]],
                         [("user", self.cl._h("看信箱")), ("assistant", self.cl._h("我看看。")),
                          ("user", self.cl._h("顺便看下垃圾箱")), ("assistant", self.cl._h("垃圾箱也空的。"))])
        self.assertTrue(led[4]["ts"] >= led[3]["ts"])
        self.assertNotIn("inflight", self.handle.meta)

    async def test_not_absorbed_gets_own_turn(self):
        t_main, main_box, c = await self._start_main()
        snap = self.HIST + [{"role": "user", "text": "看信箱", "ts": T + 1000},
                            {"role": "assistant", "text": "我看看。", "ts": T + 1001},
                            {"role": "user", "text": "顺便看下垃圾箱", "ts": T + 1005}]
        t_ij, ij_box = self._stream(snap, "ij1")
        self.assertTrue(await self._wait(lambda: len(c.queries) == 2))
        # 主轮接下来只剩文字，没有边界：主轮先收官（1 个 result），CLI 另起一轮（hook 带新 pid）
        c.feed(*_text_events("空的。"), _result("空的。"))
        await asyncio.wait_for(t_main, 3)                 # 主流当场收尾，不等插话那一轮
        await self.hook({"prompt_id": "P-new"}, None, None)
        c.feed(*_text_events("垃圾箱也看了，空的。"), _result("垃圾箱也看了，空的。"))
        await asyncio.wait_for(t_ij, 3)
        self.assertEqual([p["type"] for p in main_box if p], ["text", "text_break", "text", "done"])
        self.assertEqual([p["type"] for p in ij_box if p], ["interject", "text", "done"])
        self.assertEqual([p for p in ij_box if p][-1]["reply"], "垃圾箱也看了，空的。")
        led = self.handle.meta["ledger"]
        self.assertEqual([(e["r"], e["h"]) for e in led[2:]],
                         [("user", self.cl._h("看信箱")), ("assistant", self.cl._h("我看看。")),
                          ("assistant", self.cl._h("空的。")),
                          ("user", self.cl._h("顺便看下垃圾箱")),
                          ("assistant", self.cl._h("垃圾箱也看了，空的。"))])
        self.assertEqual(led[5]["ts"], T + 1005)        # 插话入账 ts = 发送时刻（app 不沉底）
        self.assertNotIn("inflight", self.handle.meta)
        self.assertEqual(len(self.clients), 1)

    async def test_queued_when_not_inflight_or_unsupported(self):
        # 没有在飞轮：正常排队、正常回
        t1, box1 = self._stream(self.HIST + [{"role": "user", "text": "在吗", "ts": T + 500}], "r1")
        self.assertTrue(await self._wait(lambda: self.clients and self.clients[-1].queries))
        self.clients[-1].feed(*_text_events("在。"), _result("在。"))
        await asyncio.wait_for(t1, 3)
        self.assertEqual([p["type"] for p in box1 if p], ["text", "done"])
        # 在飞但 app 没声明支持 → 排队（主轮收官后才轮到）
        hist1 = self.HIST + [{"role": "user", "text": "在吗", "ts": T + 500}, {"role": "assistant", "text": "在。", "ts": T + 501}]
        t_main, main_box, c = await self._start_main(hist1)
        snap = hist1 + [{"role": "user", "text": "看信箱", "ts": T + 1000},
                        {"role": "assistant", "text": "我看看。", "ts": T + 1001},
                        {"role": "user", "text": "再看看", "ts": T + 2005}]
        n0 = len(c.queries)                                    # r1 + main 两次 query
        t2, box2 = self._stream(snap, "r2", interject_ok=False)
        await asyncio.sleep(0.05)
        self.assertEqual(len(c.queries), n0)                   # 没写进在飞 session
        self.assertEqual(self.handle.queue.qsize(), 1)
        c.feed(*_text_events("空的。"), _result("空的。"))
        await asyncio.wait_for(t_main, 3)
        self.assertTrue(await self._wait(lambda: len(c.queries) == n0 + 1))
        c.feed(*_text_events("还是空的。"), _result("还是空的。"))
        await asyncio.wait_for(t2, 3)
        self.assertEqual([p["type"] for p in box2 if p], ["text", "done"])

    async def test_snapshot_mismatch_falls_back_to_queue(self):
        t_main, main_box, c = await self._start_main()
        snap = [{"role": "user", "text": "早", "ts": T + 0}, {"role": "assistant", "text": "改过", "ts": T + 1},
                {"role": "user", "text": "看信箱", "ts": T + 1000},
                {"role": "user", "text": "再看看", "ts": T + 1005}]
        t2, box2 = self._stream(snap, "r2")
        await asyncio.sleep(0.05)
        self.assertEqual(len(c.queries), 1)
        self.assertEqual(self.handle.queue.qsize(), 1)
        c.feed(*_text_events("空的。"), _result("空的。"))
        await asyncio.wait_for(t_main, 3)
        # 排队那轮：窗口里「改过」→ 判脏重铸（新 client）
        self.assertTrue(await self._wait(lambda: len(self.clients) == 2 and self.clients[-1].queries))
        self.clients[-1].feed(*_text_events("嗯。"), _result("嗯。"))
        await asyncio.wait_for(t2, 3)
        self.assertEqual(len(self.forged), 2)

    async def test_main_dies_pending_gets_ending(self):
        t_main, main_box, c = await self._start_main()
        snap = self.HIST + [{"role": "user", "text": "看信箱", "ts": T + 1000},
                            {"role": "user", "text": "再看看", "ts": T + 1005}]
        t_ij, ij_box = self._stream(snap, "ij1")
        self.assertTrue(await self._wait(lambda: len(c.queries) == 2))
        c.feed(_result("", is_error=True))
        await asyncio.wait_for(t_main, 3)
        await asyncio.wait_for(t_ij, 3)
        self.assertEqual([p["type"] for p in ij_box if p], ["interject", "error", "done"])
        self.assertTrue(c.disconnected)
        self.assertNotIn("inflight", self.handle.meta)

    async def test_hook_missing_twice_turns_it_off(self):
        hist = list(self.HIST)
        for i in range(2):
            t, box = self._stream(hist + [{"role": "user", "text": f"在吗{i}", "ts": T + 1000 + 10 * i}], f"r{i}")
            hist += [{"role": "user", "text": f"在吗{i}", "ts": T + 1000 + 10 * i},
                     {"role": "assistant", "text": "在。", "ts": T + 1001 + 10 * i}]
            self.assertTrue(await self._wait(lambda: self.clients and self.clients[-1].queries
                                             and len(self.clients[-1].queries) == i + 1))
            self.clients[-1].feed(*_text_events("在。"), _result("在。"))
            await asyncio.wait_for(t, 3)
        self.assertIn(self.CHAR, self.cl.INTERJECT_OFF)
        self.assertEqual(len(self.barks), 1)
        self.assertFalse(self.cl.interject_capable(self.CHAR))


if __name__ == "__main__":
    unittest.main()
