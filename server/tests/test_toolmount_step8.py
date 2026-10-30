"""PLAN_toolmount §9 第 8 步（10-30）：延迟 + 常驻（§2）、轮间隙 toggle 口子（§5.2 / §10：game 平时
off、插件开关走 toggle、关着的插件也挂）、中途探活（§5.2）、配置变了（toggle / 重铸）。
全部离线：client 是假的（记 toggle 调用、status 可编），forge / 账 / 活动账全打桩。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_toolmount_step8 -v
"""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import pipeline
import session_mgr as sm
from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import StreamEvent


# ---------- §2 开关 ----------

class DeferConfigTest(unittest.TestCase):
    def setUp(self):
        self._saved = config.CHAT_TOOL_DEFER

    def tearDown(self):
        config.CHAT_TOOL_DEFER = self._saved

    def test_parse(self):
        for v, cass, default in (("0", False, False), ("", False, False), ("1", True, True),
                                 ("all", True, True), ("cass", True, False),
                                 ("cass, default", True, True)):
            config.CHAT_TOOL_DEFER = v
            self.assertEqual(config.chat_tool_defer("cass"), cass, v)
            self.assertEqual(config.chat_tool_defer("default"), default, v)
        self.assertFalse(config.chat_tool_defer(None))

    def test_session_context_only(self):
        """chat_session 按角色问新闸；chat（-p 老路）/ wake 口径照旧。"""
        config.CHAT_TOOL_DEFER = "cass"
        self.assertTrue(pipeline.tool_search_on(pipeline.SESSION_CONTEXT, "cass"))
        self.assertFalse(pipeline.tool_search_on(pipeline.SESSION_CONTEXT, "default"))
        self.assertFalse(pipeline.tool_search_on("chat"))
        self.assertFalse(pipeline.tool_search_on("chat", "cass"))
        self.assertTrue(pipeline.tool_search_on("wake"))
        self.assertEqual(pipeline.scene_of(pipeline.SESSION_CONTEXT), "chat")
        self.assertEqual(pipeline.scene_of("wake"), "wake")
        self.assertEqual(pipeline.ALWAYS_LOAD_SERVERS, {"ombre-brain", "basics", "game"})


# ---------- build_options：延迟分支 ----------

class BuildOptionsDeferTest(unittest.TestCase):
    def setUp(self):
        self._saved = (config.CHAT_TOOL_DEFER, config.READONLY_TOOLS_ENABLED,
                       config.WRITE_TOOLS_ENABLED, config.QUESTION_CARDS_ENABLED)
        config.READONLY_TOOLS_ENABLED = config.WRITE_TOOLS_ENABLED = False
        config.QUESTION_CARDS_ENABLED = False

    def tearDown(self):
        (config.CHAT_TOOL_DEFER, config.READONLY_TOOLS_ENABLED,
         config.WRITE_TOOLS_ENABLED, config.QUESTION_CARDS_ENABLED) = self._saved

    def _build(self, char_id: str):
        import chat_loop
        import game_loop
        import plugins
        tmp = Path(tempfile.mkdtemp())
        (tmp / "persona.md").write_text("人设", "utf-8")
        calls = {"menu_ctx": None, "mounted_all": 0, "mounted": 0}
        orig = (pipeline.rendered_persona, pipeline.tool_menu_block, pipeline.sticker_block,
                plugins.mounted, plugins.mounted_all, pipeline._pet_mcp_mounted,
                pipeline._skills_mounted, pipeline._prs_mounted, pipeline.ombre_configured,
                pipeline._basics_mcp_config, chat_loop._merge_mcp_file, chat_loop._game_capable,
                game_loop.build_game_server, os.environ.pop("ANTHROPIC_API_KEY", None))

        def menu(ctx, cid=None):
            calls["menu_ctx"] = ctx
            return "■ 菜单"

        def mounted(ctx, cid=None):
            calls["mounted"] += 1
            return (str(tmp / "plug.json"), ["mcp__mail__mail_read"])

        def mounted_all(cid=None):
            calls["mounted_all"] += 1
            return (str(tmp / "plug.json"), ["mcp__mail__mail_read", "mcp__galatea__gal_read"],
                    ["galatea"])

        def merge(servers, path):
            if path.endswith("basics.json"):
                servers["basics"] = {"type": "stdio", "command": "x"}
            elif path.endswith("plug.json"):
                servers["mail"] = {"type": "stdio", "command": "m"}
                servers["galatea"] = {"type": "stdio", "command": "g"}

        pipeline.rendered_persona = lambda cid=None: tmp / "persona.md"
        pipeline.tool_menu_block = menu
        pipeline.sticker_block = lambda cat: ""
        plugins.mounted = mounted
        plugins.mounted_all = mounted_all
        pipeline._pet_mcp_mounted = lambda cid=None: False
        pipeline._skills_mounted = lambda ctx, cid=None: False
        pipeline._prs_mounted = lambda cid=None: False
        pipeline.ombre_configured = lambda cid=None: True
        pipeline._basics_mcp_config = lambda cid=None: tmp / "basics.json"
        chat_loop._merge_mcp_file = merge
        chat_loop._game_capable = lambda cid: True
        game_loop.build_game_server = lambda h, **kw: {"type": "sdk", "name": "game", "instance": None}
        try:
            h = sm.LoopHandle(char_id=char_id, scene="chat")
            opts = chat_loop.build_options(char_id, None, h)
        finally:
            (pipeline.rendered_persona, pipeline.tool_menu_block, pipeline.sticker_block,
             plugins.mounted, plugins.mounted_all, pipeline._pet_mcp_mounted,
             pipeline._skills_mounted, pipeline._prs_mounted, pipeline.ombre_configured,
             pipeline._basics_mcp_config, chat_loop._merge_mcp_file, chat_loop._game_capable,
             game_loop.build_game_server) = orig[:13]
            if orig[13] is not None:
                os.environ["ANTHROPIC_API_KEY"] = orig[13]
        return opts, h, calls

    def test_defer_on(self):
        """cass 开闸：ToolSearch 进 tools（allowed 派生）+ 环境变量 + 常驻名单 alwaysLoad +
        关着的插件也挂（mounted_all）+ 关着的名单经 meta 交给 _open_session；菜单按 chat_session 渲染。"""
        config.CHAT_TOOL_DEFER = "cass"
        opts, h, calls = self._build("cass")
        self.assertIn(pipeline.TOOL_SEARCH_TOOL, opts.tools)
        self.assertIn(pipeline.TOOL_SEARCH_TOOL, opts.allowed_tools)
        self.assertEqual(opts.env.get("ENABLE_TOOL_SEARCH"), "true")
        for n in ("ombre-brain", "basics", "game"):
            self.assertTrue(opts.mcp_servers[n].get("alwaysLoad"), n)
        for n in ("mail", "galatea"):
            self.assertNotIn("alwaysLoad", opts.mcp_servers[n], n)
        self.assertEqual(opts.mcp_servers["game"]["type"], "sdk")     # 进程内 server 也只是多一个键
        self.assertEqual(calls["mounted_all"], 1)
        self.assertEqual(calls["mounted"], 0)
        self.assertEqual(calls["menu_ctx"], pipeline.SESSION_CONTEXT)
        self.assertIn("mcp__galatea__gal_read", opts.tools)
        self.assertEqual(h.meta["mcp_plug_off"], ["galatea"])

    def test_defer_off(self):
        """闸关：一切照旧全量——没有 ToolSearch / 环境变量 / alwaysLoad，只挂启用中的插件。"""
        config.CHAT_TOOL_DEFER = "cass"
        opts, h, calls = self._build("default")
        self.assertNotIn(pipeline.TOOL_SEARCH_TOOL, opts.tools)
        self.assertEqual(opts.env, {})
        for s in opts.mcp_servers.values():
            self.assertNotIn("alwaysLoad", s)
        self.assertEqual(calls["mounted_all"], 0)
        self.assertEqual(calls["mounted"], 1)
        self.assertEqual(h.meta["mcp_plug_off"], [])
        self.assertEqual(calls["menu_ctx"], pipeline.SESSION_CONTEXT)   # 口径一样，只是延迟没开


# ---------- plugins.mounted_all ----------

class MountedAllTest(unittest.TestCase):
    def test_disabled_plugins_mounted_and_listed(self):
        import plugins
        import state_store
        tmp = Path(tempfile.mkdtemp())
        pdir = tmp / "plugins"
        for name in ("alpha", "beta", "gamma"):
            (pdir / name).mkdir(parents=True)
            (pdir / name / "main.py").write_text("", "utf-8")
            (pdir / name / "plugin.json").write_text(json.dumps(
                {"name": name, "entry": "main.py", "tools": [f"{name}_do"]}), "utf-8")
        (pdir / ".hidden").mkdir()
        orig = (plugins.PLUGINS_DIR, plugins._read_enabled, plugins.plugin_owned_by,
                state_store.char_state_dir, plugins.PLUGIN_GATE.copy())
        plugins.PLUGINS_DIR = pdir
        plugins._read_enabled = lambda cid=None: {"alpha": True, "beta": False}
        plugins.plugin_owned_by = lambda name, cid=None: name != "gamma"   # gamma 不归他
        state_store.char_state_dir = lambda cid=None: tmp
        plugins.PLUGIN_GATE.clear()
        try:
            cfg, tools, off = plugins.mounted_all("cass")
            self.assertEqual(sorted(tools), ["mcp__alpha__alpha_do", "mcp__beta__beta_do"])
            self.assertEqual(off, ["beta"])
            self.assertTrue(cfg.endswith("plugins.session.mcp.json"))
            servers = json.loads(Path(cfg).read_text("utf-8"))["mcpServers"]
            self.assertEqual(sorted(servers), ["alpha", "beta"])
            # 老口径不变：只挂启用中的，配置文件另一份
            cfg2, tools2 = plugins.mounted("chat", "cass")
            self.assertEqual(tools2, ["mcp__alpha__alpha_do"])
            self.assertTrue(cfg2.endswith("plugins.mcp.json"))
            self.assertNotEqual(cfg, cfg2)
        finally:
            (plugins.PLUGINS_DIR, plugins._read_enabled, plugins.plugin_owned_by,
             state_store.char_state_dir, gate) = orig
            plugins.PLUGIN_GATE.clear()
            plugins.PLUGIN_GATE.update(gate)


# ---------- 中途探活（§5.2） ----------

class _StatusClient:
    def __init__(self):
        self.rows: list[dict] = []
        self.reconnects: list[str] = []
        self.recon_ok = False

    async def get_mcp_status(self):
        return {"mcpServers": list(self.rows)}

    async def reconnect_mcp_server(self, name):
        self.reconnects.append(name)
        if not self.recon_ok:
            raise Exception("ECONNREFUSED: still dead")
        for r in self.rows:
            if r["name"] == name:
                r.update({"status": "connected", "tools": [{"name": "hold"}]})


def _ok(name, *tools):
    return {"name": name, "status": "connected", "tools": [{"name": t} for t in tools]}


class RecheckTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import notify
        self._bark = notify.bark_push_bg
        self.barks = []
        notify.bark_push_bg = lambda text, title=None: self.barks.append(text)

    def tearDown(self):
        import notify
        notify.bark_push_bg = self._bark

    def _handle(self):
        h = sm.LoopHandle(char_id="cass", scene="chat")
        h.meta["mcp_expected"] = {"ombre-brain": ["mcp__ombre-brain__hold"],
                                  "mail": ["mcp__mail__mail_read"]}
        h.meta["absent_tools"] = {}
        return h

    async def test_drop_then_recover_before_told_is_silent(self):
        """connected → failed：reconnect 一次，还不行进 absent + 告知；还没来得及说就恢复了 → 两句都不说。"""
        import chat_loop
        h, c = self._handle(), _StatusClient()
        c.rows = [{"name": "ombre-brain", "status": "failed", "error": "ECONNREFUSED"}, _ok("mail", "mail_read")]
        await chat_loop._recheck_mcp(c, h)
        self.assertEqual(c.reconnects, ["ombre-brain"])
        self.assertEqual(sorted(h.meta["absent_tools"]), ["ombre-brain"])
        self.assertIn("still dead", h.meta["absent_tools"]["ombre-brain"]["error"])
        self.assertEqual(h.meta["absent_note_due"], ["ombre-brain"])
        self.assertEqual(len(self.barks), 1)
        c.rows = [_ok("ombre-brain", "hold"), _ok("mail", "mail_read")]
        await chat_loop._recheck_mcp(c, h)
        self.assertEqual(h.meta["absent_tools"], {})
        self.assertNotIn("absent_note_due", h.meta)
        self.assertNotIn("absent_recovered_due", h.meta)
        self.assertEqual(chat_loop._turn_prefix(h), [])

    async def test_recover_after_told_says_back(self):
        """告知过的缺席恢复了 → 下一轮「连上了」那句（ABSENT_NOTES 第二句）；门随之放行。"""
        import chat_loop
        h, c = self._handle(), _StatusClient()
        h.meta["absent_tools"] = {"ombre-brain": {"tools": ["mcp__ombre-brain__hold"], "error": "x"}}
        gate = chat_loop._wake_gate(h)
        d = await gate({"tool_name": "mcp__ombre-brain__hold", "tool_input": {}}, "t", None)
        self.assertEqual(d["hookSpecificOutput"]["permissionDecision"], "deny")
        c.rows = [_ok("ombre-brain", "hold"), _ok("mail", "mail_read")]
        await chat_loop._recheck_mcp(c, h)
        self.assertEqual(h.meta["absent_tools"], {})
        self.assertEqual(h.meta["absent_recovered_due"], ["ombre-brain"])
        pre = chat_loop._turn_prefix(h)
        self.assertEqual(pre, ["〔" + pipeline.ABSENT_NOTES["ombre-brain"][1] + "〕"])
        self.assertEqual(await gate({"tool_name": "mcp__ombre-brain__hold", "tool_input": {}}, "t", None), {})

    async def test_off_servers_and_pending_are_not_absent(self):
        """toggle off 的 server（status=disabled）不算缺席；pending（重连中）维持上一拍的判断。"""
        import chat_loop
        h, c = self._handle(), _StatusClient()
        h.meta["mcp_off"] = {"mail"}
        c.rows = [_ok("ombre-brain", "hold"), {"name": "mail", "status": "disabled"}]
        await chat_loop._recheck_mcp(c, h)
        self.assertEqual(h.meta["absent_tools"], {})
        self.assertEqual(c.reconnects, [])
        h.meta["absent_tools"] = {"ombre-brain": {"tools": ["mcp__ombre-brain__hold"], "error": "x"}}
        c.rows = [{"name": "ombre-brain", "status": "pending"}, {"name": "mail", "status": "disabled"}]
        await chat_loop._recheck_mcp(c, h)
        self.assertEqual(sorted(h.meta["absent_tools"]), ["ombre-brain"])
        self.assertNotIn("absent_recovered_due", h.meta)

    async def test_fake_client_and_errors_are_harmless(self):
        import chat_loop
        h = self._handle()
        await chat_loop._recheck_mcp(SimpleNamespace(), h)
        await chat_loop._recheck_mcp(None, h)

        class Boom:
            async def get_mcp_status(self):
                raise RuntimeError("x")
        await chat_loop._recheck_mcp(Boom(), h)
        self.assertEqual(h.meta["absent_tools"], {})


# ---------- 轮间隙 toggle + 配置变了（loop 级） ----------

def _se(ev):
    return StreamEvent(uuid="u", session_id="s", event=ev, parent_tool_use_id=None)


def _text_events(text):
    return [_se({"type": "content_block_start", "content_block": {"type": "text"}}),
            _se({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})]


def _result(text):
    return ResultMessage(subtype="success", duration_ms=0, duration_api_ms=0, is_error=False,
                         num_turns=1, session_id="s", result=text)


class _ToggleClient:
    def __init__(self, options, owner):
        self.options = options
        self.owner = owner
        self.queries = []
        self.toggles: list[tuple[str, bool]] = []
        self.disconnected = False
        self._q: asyncio.Queue = asyncio.Queue()
        owner.clients.append(self)

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
        off = self.owner.handle.meta.get("mcp_off") or set()
        return {"mcpServers": [
            {"name": n, "status": "disabled"} if n in off else
            _ok(n, *[t.rsplit("__", 1)[-1] for t in self.options.tools if t.split("__", 2)[1] == n])
            for n in self.options.mcp_servers]}

    async def toggle_mcp_server(self, name, on):
        if self.owner.fail_toggles > 0:
            self.owner.fail_toggles -= 1
            raise RuntimeError("toggle boom")
        self.toggles.append((name, on))


SERVERS = {"basics": {}, "game": {}, "mail": {}}
TOOLS = ["mcp__basics__now", "mcp__game__game_look", "mcp__mail__mail_read"]


class ToggleLoopTest(unittest.IsolatedAsyncioTestCase):
    CHAR = "cass"

    async def asyncSetUp(self):
        import chat_loop
        import activity_log
        import game_bridge
        import cohabit_queue
        import state_store
        sm.set_event_loop(asyncio.get_running_loop())
        sm._registry.clear()
        self.clients: list[_ToggleClient] = []
        self.fail_toggles = 0
        self.sig = [(("mail", False),), True]        # 可变：配置变了 = 改它
        self._saved = (config.CHAT_TOOL_DEFER, chat_loop.forge, chat_loop._note_usage,
                       chat_loop._persist_ledger, chat_loop._ombre_on, chat_loop._frame_activities,
                       chat_loop._mount_signature, chat_loop.GAME_TICK_PAUSE, chat_loop.IDLE_CHECK_SEC,
                       activity_log.open_segment, activity_log.append_event, activity_log.close_segment,
                       activity_log.recent_shot_refs, game_bridge.release_lock,
                       cohabit_queue.code_session_closed, pipeline.mounted_tool_names,
                       state_store.read_recent_window, state_store.read_sticker_catalog)
        config.CHAT_TOOL_DEFER = "cass"
        chat_loop.forge = types.SimpleNamespace(render=lambda msgs, **kw: "sid")
        chat_loop._note_usage = lambda *a: None
        chat_loop._persist_ledger = lambda *a: None
        chat_loop._ombre_on = lambda cid: False
        chat_loop._frame_activities = lambda h, cid: h
        chat_loop._mount_signature = lambda cid: tuple(self.sig)
        chat_loop.GAME_TICK_PAUSE = 0.01
        chat_loop.IDLE_CHECK_SEC = 0.03
        activity_log.open_segment = lambda c, s, ts=None: f"{c}-{s}-1"
        activity_log.append_event = lambda seg, kind, **kw: None
        activity_log.close_segment = lambda seg, note="": None
        activity_log.recent_shot_refs = lambda seg, k: []
        game_bridge.release_lock = lambda who: None
        cohabit_queue.code_session_closed = lambda: None
        pipeline.mounted_tool_names = lambda ctx, cid=None: []
        state_store.read_recent_window = lambda cid=None: [
            {"role": "user", "text": "早", "ts": 1000}, {"role": "assistant", "text": "早", "ts": 1001}]
        state_store.read_sticker_catalog = lambda: []

        self.handle = sm.LoopHandle(char_id=self.CHAR, scene="chat")
        sm._registry[(self.CHAR, "chat")] = self.handle

        def opts(cid, catalog=None):
            # build_options 的那一笔：关着的插件名（按「当前配置」算，重铸后跟着变）
            self.handle.meta["mcp_plug_off"] = [k for k, on in self.sig[0] if not on and k in SERVERS]
            return SimpleNamespace(cwd="/tmp/nowhere", resume=None,
                                   mcp_servers=dict(SERVERS), tools=list(TOOLS))
        self.task = asyncio.create_task(chat_loop.run(
            self.handle, client_factory=lambda o: _ToggleClient(o, self), options_factory=opts))
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        import chat_loop
        import activity_log
        import game_bridge
        import cohabit_queue
        import state_store
        if not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        (config.CHAT_TOOL_DEFER, chat_loop.forge, chat_loop._note_usage,
         chat_loop._persist_ledger, chat_loop._ombre_on, chat_loop._frame_activities,
         chat_loop._mount_signature, chat_loop.GAME_TICK_PAUSE, chat_loop.IDLE_CHECK_SEC,
         activity_log.open_segment, activity_log.append_event, activity_log.close_segment,
         activity_log.recent_shot_refs, game_bridge.release_lock,
         cohabit_queue.code_session_closed, pipeline.mounted_tool_names,
         state_store.read_recent_window, state_store.read_sticker_catalog) = self._saved
        sm._registry.clear()

    async def _wait(self, cond, n=300):
        for _ in range(n):
            await asyncio.sleep(0.005)
            if cond():
                return True
        return False

    async def _open(self):
        """发一轮聊天把 session 开起来并收尾。"""
        import chat_loop
        turn = chat_loop.Turn(rid="r", history=[], new_msg={"role": "user", "text": "在吗", "ts": 2000},
                              injection="在吗", finalize=lambda reply, stored: {"reply": reply, "stored": stored})
        self.handle.queue.put_nowait(turn)
        self.assertTrue(await self._wait(lambda: self.clients and self.clients[-1].queries))
        self.clients[-1].feed(*_text_events("在。"), _result("在。"))
        while await turn.out.get() is not None:
            pass

    async def test_open_toggles_off_plugins_and_game_and_gate_speaks(self):
        import chat_loop
        await self._open()
        c = self.clients[-1]
        self.assertEqual(sorted(c.toggles), [("game", False), ("mail", False)])
        self.assertEqual(self.handle.meta["mcp_off"], {"game", "mail"})
        self.assertEqual(self.handle.meta["mcp_toggle_want"], {})
        gate = chat_loop._wake_gate(self.handle)

        def why(d):
            return d["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("不在手边", why(await gate({"tool_name": "mcp__game__game_look", "tool_input": {}}, "t", None)))
        self.assertIn("关着", why(await gate({"tool_name": "mcp__mail__mail_read", "tool_input": {}}, "t", None)))
        self.assertEqual(await gate({"tool_name": "mcp__basics__now", "tool_input": {}}, "t", None), {})
        self.assertFalse(chat_loop.game_tools_pending(self.CHAR))

    async def test_game_on_at_boundary_after_start_and_off_after_end(self):
        """game_start 置旗 → 下个轮边界 toggle on（那一轮本身工具没到手，门说人话）；game_end → off。"""
        import chat_loop
        await self._open()
        c = self.clients[-1]
        r = chat_loop.start_game_pump(self.CHAR, note="n", deliver=lambda t, s: t)
        self.assertTrue(r["ok"])
        self.assertEqual(self.handle.meta["mcp_toggle_want"], {"game": True})
        self.assertTrue(chat_loop.game_tools_pending(self.CHAR))
        gate = chat_loop._wake_gate(self.handle)
        d = await gate({"tool_name": "mcp__game__game_look", "tool_input": {}}, "t", None)
        self.assertIn("下一轮才到手", d["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertTrue(await self._wait(lambda: ("game", True) in c.toggles))
        self.assertNotIn("game", self.handle.meta["mcp_off"])
        self.assertFalse(chat_loop.game_tools_pending(self.CHAR))
        self.assertEqual(await gate({"tool_name": "mcp__game__game_look", "tool_input": {}}, "t", None), {})
        # 放下
        self.assertTrue(await self._wait(lambda: any(isinstance(q, str) for q in c.queries)))  # 一发 tick 在飞
        self.handle.meta["end_requested"] = True
        c.feed(_result("x"))
        self.assertTrue(await self._wait(lambda: self.handle.meta.get("game_pump") is None))
        self.assertTrue(await self._wait(lambda: c.toggles.count(("game", False)) == 2))
        self.assertIn("game", self.handle.meta["mcp_off"])
        self.assertFalse(c.disconnected)      # 没换 client

    async def test_toggle_failure_retries_next_boundary(self):
        import chat_loop
        self.fail_toggles = 1
        await self._open()
        c = self.clients[-1]
        # 开 session 时第一笔失败、第二笔成功；失败的那笔悬着，下个轮边界补上
        self.assertTrue(await self._wait(lambda: sorted(c.toggles) == [("game", False), ("mail", False)]))
        self.assertEqual(self.handle.meta["mcp_toggle_want"], {})
        self.assertEqual(self.handle.meta["mcp_off"], {"game", "mail"})
        chat_loop.game_tools_pending(self.CHAR)

    async def test_config_change_toggles_or_reforges(self):
        """插件在 app 里开 / 关：server 在这条 session 里 → 轮间隙 toggle、不换 client；
        新装的插件（不在 session 里）→ 轮间隙重铸（新 client）。"""
        await self._open()
        c = self.clients[-1]
        self.assertTrue(await self._wait(lambda: self.handle.meta["mcp_off"] == {"game", "mail"}))
        self.sig[0] = (("mail", True),)
        self.assertTrue(await self._wait(lambda: ("mail", True) in c.toggles))
        self.assertNotIn("mail", self.handle.meta["mcp_off"])
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.handle.meta["mount_sig"], tuple(self.sig))
        self.sig[0] = (("mail", True), ("newone", True))
        self.assertTrue(await self._wait(lambda: len(self.clients) == 2))
        self.assertTrue(c.disconnected)
        self.assertTrue(await self._wait(lambda: self.handle.meta["mcp_off"] == {"game"}))  # 重开后 mail 是开着的
        self.assertEqual(self.clients[-1].toggles, [("game", False)])


class ToggleOffCharTest(ToggleLoopTest):
    """闸没给这个角色：一切照旧——不 toggle；配置变了照样重铸（这条本来就该有）。"""
    CHAR = "default"

    async def test_open_toggles_off_plugins_and_game_and_gate_speaks(self):
        await self._open()
        self.assertEqual(self.clients[-1].toggles, [])
        self.assertEqual(self.handle.meta["mcp_off"], set())

    async def test_game_on_at_boundary_after_start_and_off_after_end(self):
        import chat_loop
        await self._open()
        chat_loop.start_game_pump(self.CHAR, note="n", deliver=lambda t, s: t)
        self.assertEqual(self.handle.meta.get("mcp_toggle_want"), {})
        self.assertFalse(chat_loop.game_tools_pending(self.CHAR))

    async def test_toggle_failure_retries_next_boundary(self):
        pass

    async def test_config_change_toggles_or_reforges(self):
        await self._open()
        self.sig[0] = (("mail", True),)
        self.assertTrue(await self._wait(lambda: len(self.clients) == 2))
        self.assertEqual(self.clients[-1].toggles, [])


if __name__ == "__main__":
    unittest.main()
