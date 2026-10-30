"""PLAN_toolmount §9 第 9 步：重铸留用法（§6）。

记账（_ToolTrace → state_store.touch_tools）、锚点两条规则（_anchor_index）、铸回
（_merge_tool_loads / _loadable_tools）、forge 的 tools 槽允许 content 列表、token 表缓存
（_cache_tool_tokens）、整条链（轮里取过用法 → 重铸时 forge 收到 ToolSearch→tool_reference）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_toolmount_step9 -v
"""
import asyncio
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import session_mgr as sm
from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import (AssistantMessage, StreamEvent, TextBlock,
                                    ToolResultBlock, ToolUseBlock, UserMessage)

ECHO = "mcp__mail__mail_read"
SEND = "mcp__mail__mail_send"
NAV = "mcp__browser__browser_navigate"
ALARM = "mcp__basics__next_wake"


class _StateTmp(unittest.TestCase):
    """state 根指临时目录（单测绝不写真 state）。"""

    def setUp(self):
        import state_store
        self.ss = state_store
        self.tmp = Path(tempfile.mkdtemp(prefix="step9_"))
        self._root = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"

    def tearDown(self):
        self.ss.CHAR_STATE_ROOT = self._root
        shutil.rmtree(self.tmp, ignore_errors=True)


class TouchLedgerTest(_StateTmp):
    def test_last_touch_wins_and_carries_turn(self):
        self.ss.touch_tools([ECHO], "cass", ts=1000, turn_kind="wake", turn_ts=990)
        self.ss.touch_tools([ECHO, NAV], "cass", ts=2000, turn_kind="chat")
        d = self.ss.read_tool_touch("cass")
        self.assertEqual(d[ECHO], {"ts": 2000, "turn_kind": "chat", "turn_ts": 0})
        self.assertEqual(d[NAV]["ts"], 2000)
        self.assertEqual(self.ss.read_tool_touch("default"), {})     # 按角色分

    def test_cap_keeps_the_most_recent(self):
        orig = self.ss.TOOL_TOUCH_CAP
        self.ss.TOOL_TOUCH_CAP = 3
        try:
            for i in range(5):
                self.ss.touch_tools([f"mcp__x__t{i}"], "cass", ts=1000 + i)
            d = self.ss.read_tool_touch("cass")
            self.assertEqual(sorted(d), ["mcp__x__t2", "mcp__x__t3", "mcp__x__t4"])
        finally:
            self.ss.TOOL_TOUCH_CAP = orig

    def test_empty_and_missing(self):
        self.ss.touch_tools([], "cass")
        self.ss.touch_tools([None, ""], "cass")
        self.assertEqual(self.ss.read_tool_touch("cass"), {})


class ToolTraceTouchTest(_StateTmp):
    """记账挂在 _ToolTrace：用到（tool_use）和取到（ToolSearch 的 tool_reference）都算接触；
    game 不记、内置工具不记；轮来源随手记；闹钟留底也补 turn_kind / turn_ts。"""

    def setUp(self):
        super().setUp()
        import activity_log
        import chat_loop
        self.cl = chat_loop
        self._al = (activity_log.append_act, activity_log.append_event)
        activity_log.append_act = lambda *a, **k: None
        activity_log.append_event = lambda *a, **k: None

    def tearDown(self):
        import activity_log
        activity_log.append_act, activity_log.append_event = self._al
        super().tearDown()

    def _trace(self, kind="chat", turn_ts=0):
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["turn_kind"] = kind
        if turn_ts:
            handle.meta["turn_ts"] = turn_ts
        return self.cl._ToolTrace(handle)

    def test_use_touches_mcp_only(self):
        tr = self._trace()
        tr.use("t1", NAV, {"url": "x"})
        tr.use("t2", "Read", {"file_path": "/x"})
        tr.use("t3", "mcp__game__game_tap", {})
        tr.use("t4", "ToolSearch", {"query": "select:" + ECHO})
        d = self.ss.read_tool_touch("cass")
        self.assertEqual(sorted(d), [NAV])
        self.assertEqual(d[NAV]["turn_kind"], "chat")

    def test_toolsearch_result_touches_references(self):
        tr = self._trace("wake", turn_ts=5000)
        tr.use("t1", "ToolSearch", {"query": f"select:{ECHO},{SEND}"})
        tr.result("t1", False, [{"type": "tool_reference", "tool_name": ECHO},
                                {"type": "tool_reference", "tool_name": SEND},
                                {"type": "tool_reference", "tool_name": "mcp__game__game_tap"}])
        d = self.ss.read_tool_touch("cass")
        self.assertEqual(sorted(d), [ECHO, SEND])
        self.assertEqual(d[ECHO]["turn_kind"], "wake")
        self.assertEqual(d[ECHO]["turn_ts"], 5000)

    def test_alarm_record_carries_turn_fields(self):
        tr = self._trace("wake", turn_ts=7000)
        tr.use("a1", ALARM, {"action": "set", "minutes": 30, "todo": "回信"})
        tr.result("a1", False, "定在 00:50")
        rows = self.ss.read_alarm_calls("cass")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_kind"], "wake")
        self.assertEqual(rows[0]["turn_ts"], 7000)


class AnchorTest(unittest.TestCase):
    """锚点按轮来源分两条（§6 复审）；老记录照旧「最近的一条、容差 120s」。"""

    def setUp(self):
        import chat_loop
        self.cl = chat_loop
        self.hist = [
            {"role": "user", "text": "a", "ts": 1000},
            {"role": "assistant", "text": "b", "ts": 1010},      # 上一轮的回复
            {"role": "user", "text": "c", "ts": 1030},
            {"role": "assistant", "text": "d1", "ts": 1045},     # 本轮第一段（工具之后）
            {"role": "user", "text": "插话", "ts": 1050},
            {"role": "assistant", "text": "d2", "ts": 1060},     # 插话之后那段
            {"role": "assistant", "text": "醒来说的", "ts": 2000},  # 醒来投递，ts=started_ts
        ]
        self.spots = chat_loop._assistant_spots(self.hist)

    def test_chat_first_segment_after_touch_not_the_latest(self):
        i = self.cl._anchor_index(self.spots, {"ts": 1040, "turn_kind": "chat"})
        self.assertEqual(self.hist[i]["text"], "d1")

    def test_chat_does_not_fall_back_to_previous_turn(self):
        """早侧只容钟差：接触在 1040，上一轮回复 1010 不能被挑中（闹钟那 120s 容差会挑到它）。"""
        i = self.cl._anchor_index(self.spots, {"ts": 1040, "turn_kind": "chat"})
        self.assertNotEqual(self.hist[i]["text"], "b")
        # 钟差：app 的气泡 ts 比接触早 3 秒仍算这轮
        i = self.cl._anchor_index(self.spots, {"ts": 1048, "turn_kind": "chat"})
        self.assertEqual(self.hist[i]["text"], "d1")

    def test_chat_rolled_out_of_window(self):
        """接触之后第一段离得太远 = 那轮回复不在窗口里 → 不铸。"""
        self.assertIsNone(self.cl._anchor_index(self.spots, {"ts": 1200, "turn_kind": "chat"}))
        self.assertIsNone(self.cl._anchor_index(self.spots, {"ts": 3000, "turn_kind": "chat"}))

    def test_wake_exact_match_only(self):
        i = self.cl._anchor_index(self.spots, {"ts": 2010, "turn_kind": "wake", "turn_ts": 2000})
        self.assertEqual(self.hist[i]["text"], "醒来说的")
        self.assertIsNone(self.cl._anchor_index(self.spots, {"ts": 2010, "turn_kind": "wake", "turn_ts": 2001}))
        self.assertIsNone(self.cl._anchor_index(self.spots, {"ts": 2010, "turn_kind": "wake", "turn_ts": 0}))

    def test_legacy_record_nearest_within_slack(self):
        i = self.cl._anchor_index(self.spots, {"ts": 1005})
        self.assertEqual(self.hist[i]["text"], "b")
        self.assertIsNone(self.cl._anchor_index(self.spots, {"ts": 1500}))
        self.assertIsNone(self.cl._anchor_index(self.spots, {"ts": 0}))
        self.assertIsNone(self.cl._anchor_index([], {"ts": 1005}))


class MergeToolLoadsTest(_StateTmp):
    def setUp(self):
        super().setUp()
        import chat_loop
        self.cl = chat_loop
        self.hist = [
            {"role": "user", "text": "a", "ts": 1000},
            {"role": "assistant", "text": "b", "ts": 1010},
            {"role": "user", "text": "c", "ts": 1030},
            {"role": "assistant", "text": "d", "ts": 1045},
        ]

    def test_groups_into_one_toolsearch_per_assistant(self):
        self.ss.touch_tools([ECHO], "cass", ts=1005, turn_kind="chat")
        self.ss.touch_tools([SEND], "cass", ts=1040, turn_kind="chat")
        self.ss.touch_tools([NAV], "cass", ts=1041, turn_kind="chat")
        out = self.cl._merge_tool_loads(self.hist, "cass", allowed={ECHO, SEND, NAV})
        self.assertNotIn("tools", out[0])
        self.assertEqual(out[1]["tools"][0]["input"]["query"], "select:" + ECHO)
        tc = out[3]["tools"][0]
        self.assertEqual(tc["name"], "ToolSearch")
        self.assertEqual(tc["input"], {"query": f"select:{SEND},{NAV}", "max_results": 2})
        self.assertEqual(tc["result"], [{"type": "tool_reference", "tool_name": SEND},
                                        {"type": "tool_reference", "tool_name": NAV}])
        self.assertEqual(len(out[3]["tools"]), 1)
        self.assertEqual(self.hist[3].get("tools"), None)      # 不动输入

    def test_only_allowed_and_in_window(self):
        self.ss.touch_tools([ECHO, SEND], "cass", ts=1040, turn_kind="chat")
        self.ss.touch_tools([NAV], "cass", ts=5000, turn_kind="chat")      # 窗口外
        out = self.cl._merge_tool_loads(self.hist, "cass", allowed={ECHO, NAV})
        self.assertEqual(out[3]["tools"][0]["result"],
                         [{"type": "tool_reference", "tool_name": ECHO}])
        # 闸关 = allowed 空 = 原样
        self.assertIs(self.cl._merge_tool_loads(self.hist, "cass", allowed=set()), self.hist)
        # 没账 = 原样
        self.assertIs(self.cl._merge_tool_loads(self.hist, "default", allowed={ECHO}), self.hist)

    def test_budget_most_recent_first(self):
        orig = self.cl.TOOL_LOAD_BUDGET_TOKENS
        self.cl.TOOL_LOAD_BUDGET_TOKENS = 1000
        try:
            self.ss.touch_tools([ECHO], "cass", ts=1038, turn_kind="chat")
            self.ss.touch_tools([SEND], "cass", ts=1039, turn_kind="chat")
            self.ss.touch_tools([NAV], "cass", ts=1040, turn_kind="chat")
            out = self.cl._merge_tool_loads(self.hist, "cass", allowed={ECHO, SEND, NAV},
                                            tokens={NAV: 600, SEND: 300, ECHO: 300})
            names = [b["tool_name"] for b in out[3]["tools"][0]["result"]]
            self.assertEqual(names, [SEND, NAV])        # ECHO 最老，预算到顶被截
            # 没有 token 表：按 fallback 估
            out = self.cl._merge_tool_loads(self.hist, "cass", allowed={ECHO, SEND, NAV})
            self.assertEqual(len(out[3]["tools"][0]["result"]), 2)
        finally:
            self.cl.TOOL_LOAD_BUDGET_TOKENS = orig

    def test_toolsearch_rides_before_alarm_on_same_turn(self):
        self.ss.touch_tools([ECHO], "cass", ts=1040, turn_kind="chat")
        self.ss.append_alarm_call({"ts": 1041, "id": "a1", "name": ALARM, "ok": True,
                                   "ret": "定了", "input": {"action": "set"},
                                   "turn_kind": "chat", "turn_ts": 0}, "cass")
        out = self.cl._merge_tool_loads(
            self.cl._merge_alarm_calls(self.hist, "cass"), "cass", allowed={ECHO})
        self.assertEqual([t["name"] for t in out[3]["tools"]], ["ToolSearch", ALARM])

    def test_alarm_with_turn_fields_uses_new_anchor(self):
        """老记录挂最近一条；带 turn_kind 的走两条新规则（醒来轮精确匹配）。"""
        hist = self.hist + [{"role": "assistant", "text": "醒来说的", "ts": 2000}]
        self.ss.append_alarm_call({"ts": 2030, "id": "w1", "name": ALARM, "ok": True,
                                   "ret": "定了", "input": {"action": "set"},
                                   "turn_kind": "wake", "turn_ts": 2000}, "cass")
        self.ss.append_alarm_call({"ts": 1012, "id": "old", "name": ALARM, "ok": True,
                                   "ret": "撤了", "input": {"action": "clear"}}, "cass")
        out = self.cl._merge_alarm_calls(hist, "cass")
        self.assertEqual(out[4]["tools"][0]["input"], {"action": "set"})
        self.assertEqual(out[1]["tools"][0]["input"], {"action": "clear"})


class LoadableToolsTest(unittest.TestCase):
    def test_filters(self):
        import chat_loop
        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["mcp_plug_off"] = ["browser"]
        opts = SimpleNamespace(tools=["ToolSearch", ECHO, SEND, NAV, "mcp__ombre-brain__breath",
                                     "mcp__game__game_tap", "mcp__basics__now", "Read"])
        self.assertEqual(chat_loop._loadable_tools(opts, handle), {ECHO, SEND})
        # 没挂 ToolSearch（闸关）= 空集
        opts2 = SimpleNamespace(tools=[ECHO, SEND])
        self.assertEqual(chat_loop._loadable_tools(opts2, handle), set())
        self.assertEqual(chat_loop._loadable_tools(SimpleNamespace(), handle), set())


class ForgeToolRefTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="step9forge_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render(self, msgs):
        import forge
        sid = forge.render(msgs, cwd="/probe/cwd", projects_root=self.tmp)
        p = forge.transcript_path("/probe/cwd", sid, self.tmp)
        return sid, [json.loads(l) for l in p.read_text("utf-8").splitlines() if l.strip()]

    def test_reference_list_rides_as_content_with_tool_use_result(self):
        refs = [{"type": "tool_reference", "tool_name": ECHO},
                {"type": "tool_reference", "tool_name": SEND}]
        sid, evs = self._render([
            {"role": "user", "text": "看一眼信箱", "ts": 1000},
            {"role": "assistant", "text": "看了。",
             "tools": [{"name": "ToolSearch",
                        "input": {"query": f"select:{ECHO},{SEND}", "max_results": 2},
                        "result": refs}]}])
        self.assertEqual([e["type"] for e in evs], ["user", "assistant", "user", "assistant"])
        tu = evs[1]["message"]["content"][0]
        self.assertEqual((tu["type"], tu["name"]), ("tool_use", "ToolSearch"))
        tr = evs[2]["message"]["content"][0]
        self.assertEqual(tr["content"], refs)             # 列表照原样，不 str()
        self.assertEqual(tr["tool_use_id"], tu["id"])
        self.assertEqual(evs[2]["toolUseResult"],
                         {"matches": [ECHO, SEND], "query": f"select:{ECHO},{SEND}",
                          "total_deferred_tools": 0})
        # 确定性 + 工具参与 digest
        sid2, _ = self._render([
            {"role": "user", "text": "看一眼信箱", "ts": 1000},
            {"role": "assistant", "text": "看了。",
             "tools": [{"name": "ToolSearch",
                        "input": {"query": f"select:{ECHO},{SEND}", "max_results": 2},
                        "result": refs}]}])
        self.assertEqual(sid, sid2)
        sid3, _ = self._render([
            {"role": "user", "text": "看一眼信箱", "ts": 1000},
            {"role": "assistant", "text": "看了。",
             "tools": [{"name": "ToolSearch",
                        "input": {"query": f"select:{ECHO},{SEND}", "max_results": 2},
                        "result": refs[:1]}]}])
        self.assertNotEqual(sid, sid3)

    def test_mixed_tools_no_tool_use_result_and_string_still_string(self):
        _, evs = self._render([
            {"role": "user", "text": "x", "ts": 1000},
            {"role": "assistant", "text": "y",
             "tools": [{"name": "ToolSearch", "input": {"query": "select:" + ECHO, "max_results": 1},
                        "result": [{"type": "tool_reference", "tool_name": ECHO}]},
                       {"name": ALARM, "input": {"action": "set"}, "result": "定了"}]}])
        self.assertNotIn("toolUseResult", evs[2])
        self.assertEqual(evs[2]["message"]["content"][1]["content"], "定了")

    def test_bad_list_rejected(self):
        import forge
        with self.assertRaises(ValueError):
            forge.render([{"role": "user", "text": "x", "ts": 1},
                          {"role": "assistant", "text": "y",
                           "tools": [{"name": "ToolSearch", "input": {}, "result": ["not a block"]}]}],
                         cwd="/probe/cwd", projects_root=self.tmp)


class CacheTokensTest(unittest.IsolatedAsyncioTestCase):
    async def test_cache_once_and_only_when_loadable(self):
        import chat_loop
        calls = []

        class C:
            async def get_context_usage(self):
                calls.append(1)
                return {"mcpTools": [{"name": ECHO, "serverName": "mail", "tokens": 321},
                                     {"name": NAV, "tokens": 0}]}

        handle = sm.LoopHandle(char_id="cass", scene="chat")
        await chat_loop._cache_tool_tokens(C(), handle)          # 没可铸工具：不拍
        self.assertEqual(calls, [])
        handle.meta["tool_loadable"] = {ECHO}
        await chat_loop._cache_tool_tokens(C(), handle)
        self.assertEqual(handle.meta["tool_tokens"], {ECHO: 321, NAV: 0})
        await chat_loop._cache_tool_tokens(C(), handle)          # 拍过就不拍
        self.assertEqual(calls, [1])
        # client 没这个方法（旧 SDK / 假 client）：静默
        h2 = sm.LoopHandle(char_id="cass", scene="chat")
        h2.meta["tool_loadable"] = {ECHO}
        await chat_loop._cache_tool_tokens(SimpleNamespace(), h2)
        self.assertNotIn("tool_tokens", h2.meta)

    async def test_failure_is_logged_not_raised(self):
        import chat_loop

        class Bad:
            async def get_context_usage(self):
                raise RuntimeError("boom")

        handle = sm.LoopHandle(char_id="cass", scene="chat")
        handle.meta["tool_loadable"] = {ECHO}
        await chat_loop._cache_tool_tokens(Bad(), handle)
        self.assertNotIn("tool_tokens", handle.meta)


# ---------- 整条链：轮里取过用法 → 重铸时 forge 收到 ToolSearch→tool_reference ----------

def _se(event):
    return StreamEvent(uuid="u", session_id="s", event=event)


def _text_events(text):
    return [_se({"type": "content_block_start", "content_block": {"type": "text"}}),
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})]


def _result(text):
    return ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0, is_error=False,
                         num_turns=1, session_id="s", result=text)


class _Client:
    def __init__(self, options, owner):
        self.options = options
        self.owner = owner
        self.queries = []
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

    async def get_mcp_status(self):
        return {"mcpServers": [{"name": "mail", "status": "connected",
                                "tools": [{"name": "mail_read"}, {"name": "mail_send"}]}]}

    async def get_context_usage(self):
        self.owner.usage_calls += 1
        return {"mcpTools": [{"name": ECHO, "tokens": 300}, {"name": SEND, "tokens": 400}]}


class ReforgeKeepsLoadsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import chat_loop
        import state_store
        import activity_log
        self.cl = chat_loop
        sm.set_event_loop(asyncio.get_running_loop())
        sm._registry.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="step9loop_"))
        self.clients: list[_Client] = []
        self.forged: list[list[dict]] = []
        self.usage_calls = 0
        self._saved = (state_store.CHAR_STATE_ROOT, config.CHAT_TOOL_DEFER, chat_loop.forge,
                       chat_loop._note_usage, chat_loop._persist_ledger, chat_loop._ombre_on,
                       chat_loop._frame_activities, chat_loop._mount_signature,
                       activity_log.append_act, activity_log.append_event)
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        config.CHAT_TOOL_DEFER = "cass"
        chat_loop.forge = types.SimpleNamespace(
            render=lambda msgs, **kw: (self.forged.append(list(msgs)), f"sid-{len(self.forged)}")[1])
        chat_loop._note_usage = lambda *a: None
        chat_loop._persist_ledger = lambda *a: None
        chat_loop._ombre_on = lambda cid: False
        chat_loop._frame_activities = lambda h, cid: h
        chat_loop._mount_signature = lambda cid: ((), True)
        activity_log.append_act = lambda *a, **k: None
        activity_log.append_event = lambda *a, **k: None
        self.handle = sm.LoopHandle(char_id="cass", scene="chat")
        sm._registry[("cass", "chat")] = self.handle
        opts = lambda cid, catalog=None: SimpleNamespace(
            cwd="/tmp/nowhere", resume=None, mcp_servers={"mail": {}},
            tools=["ToolSearch", ECHO, SEND])
        self.task = asyncio.create_task(chat_loop.run(
            self.handle, client_factory=lambda o: self._mk(o), options_factory=opts))
        await asyncio.sleep(0)

    def _mk(self, options):
        c = _Client(options, self)
        self.clients.append(c)
        return c

    async def asyncTearDown(self):
        import chat_loop
        import state_store
        import activity_log
        if not self.task.done():
            self.handle.stop_reason = "test-teardown"
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        (state_store.CHAR_STATE_ROOT, config.CHAT_TOOL_DEFER, chat_loop.forge,
         chat_loop._note_usage, chat_loop._persist_ledger, chat_loop._ombre_on,
         chat_loop._frame_activities, chat_loop._mount_signature,
         activity_log.append_act, activity_log.append_event) = self._saved
        sm._registry.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _turn(self, history, text, ts):
        return self.cl.Turn(rid=f"r{ts}", history=self.cl.norm_history(history),
                            new_msg={"role": "user", "text": text, "ts": ts},
                            injection=text,
                            finalize=lambda reply, stored: {"reply": reply, "stored": stored})

    async def _play(self, turn, *feed):
        self.handle.queue.put_nowait(turn)
        for _ in range(50):
            await asyncio.sleep(0.005)
            if self.clients and self.clients[-1].queries:
                break
        self.clients[-1].feed(*feed)
        while await asyncio.wait_for(turn.out.get(), timeout=2) is not None:
            pass

    async def test_loads_touched_in_a_turn_come_back_on_reforge(self):
        hist = [{"role": "user", "text": "早", "ts": 1000}, {"role": "assistant", "text": "早", "ts": 1001}]
        import time
        t_now = int(time.time())
        # 第一轮：他 ToolSearch 取了 mail_read 的用法，然后调它，然后回话
        await self._play(
            self._turn(hist, "看看信箱", t_now),
            AssistantMessage(content=[ToolUseBlock(id="s1", name="ToolSearch",
                                                   input={"query": "select:" + ECHO, "max_results": 1})],
                             model="m"),
            UserMessage(content=[ToolResultBlock(tool_use_id="s1", content=[
                {"type": "tool_reference", "tool_name": ECHO}])]),
            AssistantMessage(content=[ToolUseBlock(id="u1", name=ECHO, input={})], model="m"),
            UserMessage(content=[ToolResultBlock(tool_use_id="u1", content="没新信")]),
            AssistantMessage(content=[TextBlock(text="没新信。")], model="m"),
            *_text_events("没新信。"), _result("没新信。"))
        import state_store
        touched = state_store.read_tool_touch("cass")
        self.assertIn(ECHO, touched)
        self.assertEqual(touched[ECHO]["turn_kind"], "chat")
        self.assertEqual(self.usage_calls, 1)                 # 首轮之后拍了一次 token 表
        self.assertEqual(self.handle.meta["tool_tokens"], {ECHO: 300, SEND: 400})
        self.assertEqual(self.handle.meta["tool_loadable"], {ECHO, SEND})
        self.assertEqual(len(self.forged), 1)
        self.assertNotIn("tools", self.forged[0][1])          # 开场铸造时账还是空的
        # 第二轮：TA 改了历史 → 判脏重铸；窗口里本轮的回复 ts 在接触之后 → 用法铸回那条上
        hist2 = [{"role": "user", "text": "早", "ts": 1000}, {"role": "assistant", "text": "改了", "ts": 1001},
                 {"role": "user", "text": "看看信箱", "ts": t_now},
                 {"role": "assistant", "text": "没新信。", "ts": t_now + 2}]
        await self._play(self._turn(hist2, "嗯", t_now + 10), *_text_events("嗯。"), _result("嗯。"))
        self.assertEqual(len(self.clients), 2)
        self.assertEqual(len(self.forged), 2)
        tools = self.forged[1][3].get("tools") or []
        self.assertEqual([t["name"] for t in tools], ["ToolSearch"])
        self.assertEqual(tools[0]["input"]["query"], "select:" + ECHO)
        self.assertEqual(tools[0]["result"], [{"type": "tool_reference", "tool_name": ECHO}])
        self.assertNotIn("tools", self.forged[1][1])
        self.assertEqual(self.usage_calls, 1)                 # 跨重开留着，不再拍


class ReforgeGateOffTest(ReforgeKeepsLoadsTest):
    """闸关（session 没挂 ToolSearch）：账照记、铸回不做——铸一个 session 里没有的工具的调用是编。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        config.CHAT_TOOL_DEFER = "0"
        # 换 options：没有 ToolSearch
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass
        opts = lambda cid, catalog=None: SimpleNamespace(
            cwd="/tmp/nowhere", resume=None, mcp_servers={"mail": {}}, tools=[ECHO, SEND])
        self.task = asyncio.create_task(self.cl.run(
            self.handle, client_factory=lambda o: self._mk(o), options_factory=opts))
        await asyncio.sleep(0)

    async def test_loads_touched_in_a_turn_come_back_on_reforge(self):
        import time
        import state_store
        t_now = int(time.time())
        hist = [{"role": "user", "text": "早", "ts": 1000}, {"role": "assistant", "text": "早", "ts": 1001}]
        await self._play(
            self._turn(hist, "看看信箱", t_now),
            AssistantMessage(content=[ToolUseBlock(id="u1", name=ECHO, input={})], model="m"),
            UserMessage(content=[ToolResultBlock(tool_use_id="u1", content="没新信")]),
            *_text_events("没新信。"), _result("没新信。"))
        self.assertIn(ECHO, state_store.read_tool_touch("cass"))
        self.assertEqual(self.handle.meta["tool_loadable"], set())
        self.assertEqual(self.usage_calls, 0)
        hist2 = [{"role": "user", "text": "早", "ts": 1000}, {"role": "assistant", "text": "改了", "ts": 1001},
                 {"role": "user", "text": "看看信箱", "ts": t_now},
                 {"role": "assistant", "text": "没新信。", "ts": t_now + 2}]
        await self._play(self._turn(hist2, "嗯", t_now + 10), *_text_events("嗯。"), _result("嗯。"))
        self.assertEqual(len(self.forged), 2)
        self.assertNotIn("tools", self.forged[1][3])


if __name__ == "__main__":
    unittest.main()
