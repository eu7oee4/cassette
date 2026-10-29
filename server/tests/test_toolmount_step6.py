"""PLAN_toolmount §9 第 6 步（10-29）：固定菜单按配置渲染（§3.1/§3.2）、瘦身第二刀（§3.3）、
开 session 探活 + 缺席门 + 告知（§5.1）、醒来轮也弹卡 + TA 来了让路（10-29 机主拍板）。
全部离线：菜单文件写进临时目录，mounted_tool_names / permits.ask / client 都打桩——
这些路由背后是生产状态文件（tests-reading-prod-state 雷）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_toolmount_step6 -v
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import characters
import pipeline
import permits
import questions
import session_mgr as sm

MENU = """<!-- 头注释 -->
## 看信 <!-- needs: mail_read -->
看看收件箱。

## 发信 <!-- needs: mail_send -->
写一封出去。

## 翻代码和资料 <!-- needs: Read, Grep, Glob -->
随手翻。

## 动手改东西 <!-- needs: Edit, Write, NotebookEdit, Bash -->
弹卡。

## 拿不准的事让 TA 选 <!-- needs: AskUserQuestion -->
出卡。

## 幽灵 <!-- needs: ghost_tool -->
配置里没有这个。
"""

CHAT_MOUNTED = ["mcp__basics__now", "mcp__mail__mail_read", "mcp__mail__mail_send"]
WAKE_MOUNTED = ["mcp__basics__now", "mcp__mail__mail_read"]


class FlagsMixin:
    def setUp(self):
        self._saved = (config.READONLY_TOOLS_ENABLED, config.WRITE_TOOLS_ENABLED,
                       config.QUESTION_CARDS_ENABLED, config.CHAT_ENGINE)
        config.READONLY_TOOLS_ENABLED = True
        config.WRITE_TOOLS_ENABLED = True
        config.QUESTION_CARDS_ENABLED = True
        config.CHAT_ENGINE = "sdk"
        permits._pending.clear()
        questions._pending.clear()
        import notify
        self._bark_orig = notify.bark_push
        notify.bark_push = lambda text, title=None: True     # 别真推公网

    def tearDown(self):
        import notify
        notify.bark_push = self._bark_orig
        (config.READONLY_TOOLS_ENABLED, config.WRITE_TOOLS_ENABLED,
         config.QUESTION_CARDS_ENABLED, config.CHAT_ENGINE) = self._saved
        permits._pending.clear()
        questions._pending.clear()


class MenuRenderTest(FlagsMixin, unittest.TestCase):
    """§3.1 按配置渲染：内置工具按开关并进全集；场景注记 / 路径行自动补；反向喊话。"""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "tool_menu.md").write_text(MENU, "utf-8")
        import plugins
        self._orig = (characters.tool_menu_path, pipeline.mounted_tool_names,
                      plugins.shadowed_tools, pipeline.logerr)
        characters.tool_menu_path = lambda cid=None: self.tmp / "tool_menu.md"
        pipeline.mounted_tool_names = lambda ctx, cid=None: (
            list(WAKE_MOUNTED) if ctx == "wake" else list(CHAT_MOUNTED))
        plugins.shadowed_tools = lambda ctx, cid=None: []
        self.logs: list[str] = []
        pipeline.logerr = lambda msg: self.logs.append(str(msg))
        pipeline._menu_gap_seen.clear()

    def tearDown(self):
        import plugins
        (characters.tool_menu_path, pipeline.mounted_tool_names,
         plugins.shadowed_tools, pipeline.logerr) = self._orig
        pipeline._menu_gap_seen.clear()
        super().tearDown()

    def test_configured_adds_builtins_by_flags(self):
        names = pipeline.configured_tool_names("chat", "cass")
        for t in ("Read", "Grep", "Glob", "Edit", "Write", "NotebookEdit", "Bash",
                  "AskUserQuestion"):
            self.assertIn(t, names)
        self.assertNotIn("Read", pipeline.mounted_tool_names("chat", "cass"))   # 醒来允许集不掺
        config.QUESTION_CARDS_ENABLED = False
        self.assertNotIn("AskUserQuestion", pipeline.configured_tool_names("chat", "cass"))
        config.CHAT_ENGINE = "p"
        self.assertNotIn("Read", pipeline.configured_tool_names("chat", "cass"))

    def test_chat_render_blocks_notes_and_paths(self):
        out = pipeline.tool_menu_block("chat", "cass")
        for title in ("看信", "发信", "翻代码和资料", "动手改东西", "拿不准的事让 TA 选"):
            self.assertIn(f"■ {title}", out)
        self.assertNotIn("幽灵", out)
        # 场景注记：mail_send 不在 wake 全集 → 发信块尾自动补；读信 / 写类（醒来也弹卡）不补
        self.assertIn("醒着的时候这几样用不了：mail_send", out)
        self.assertEqual(out.count("醒着的时候这几样用不了"), 1)
        # 翻代码那块自动补两个仓的路径
        self.assertIn(f"范围：cassette 仓 {pipeline.CODE_ROOT}", out)
        self.assertIn(str(pipeline.DOSSIER_ROOT), out)
        # 反向喊话：菜单提到配置里没有的 ghost_tool
        self.assertTrue(any("ghost_tool" in m for m in self.logs), self.logs)

    def test_wake_render_drops_blocks_without_note(self):
        out = pipeline.tool_menu_block("wake", "cass")
        self.assertIn("■ 看信", out)
        self.assertNotIn("■ 发信", out)
        self.assertNotIn("醒着的时候这几样用不了", out)
        self.assertIn("■ 动手改东西", out)      # 写类醒来也在全集里（卡是它的闸）

    def test_question_block_follows_flag(self):
        config.QUESTION_CARDS_ENABLED = False
        self.assertNotIn("拿不准的事让 TA 选", pipeline.tool_menu_block("chat", "cass"))

    def test_render_is_pure_in_configuration(self):
        """同一份配置两次渲染一字不差（前缀缓存的前提，§3.1）。"""
        a = pipeline.tool_menu_block("chat", "cass")
        b = pipeline.tool_menu_block("chat", "cass")
        self.assertEqual(a, b)


class AbsentNoteTest(unittest.TestCase):
    def test_table_and_fallback(self):
        s = pipeline.absent_note(["ombre-brain", "foo"])
        self.assertIn("〔记忆那边这会儿够不着", s)
        self.assertIn("〔foo 这会儿够不着〕", s)
        self.assertIn("又够得着了", pipeline.absent_note(["ombre-brain"], recovered=True))
        self.assertEqual(pipeline.absent_note([]), "")


class _FakeClient:
    """get_mcp_status 的形状照 tools/mcp_probe_ombre_dead.py 实测：failed 没有 tools 键；
    reconnect 对连不上的 server 是抛异常。"""

    def __init__(self):
        self.calls = 0
        self.reconnects: list[str] = []

    async def get_mcp_status(self):
        self.calls += 1
        mail = ({"name": "mail", "status": "pending"} if self.calls == 1 else
                {"name": "mail", "status": "connected",
                 "tools": [{"name": "mcp__mail__mail_read"}, {"name": "mail_send"}]})
        return {"mcpServers": [
            mail,
            {"name": "ombre-brain", "status": "failed", "error": "ECONNREFUSED: 127.0.0.1:18001"},
            {"name": "basics", "status": "connected", "tools": [{"name": "now"}]},   # 少 fetch
        ]}

    async def reconnect_mcp_server(self, name):
        self.reconnects.append(name)
        raise Exception("ECONNREFUSED: still dead")


class ProbeTest(unittest.IsolatedAsyncioTestCase):
    """§5.1：拍到无 pending → failed 的 reconnect 一次（异常原话） → 对账 → meta + 告知 + 门。"""

    def _opts(self):
        return SimpleNamespace(tools=["mcp__mail__mail_read", "mcp__mail__mail_send",
                                      "mcp__ombre-brain__hold", "mcp__ombre-brain__breath",
                                      "mcp__basics__now", "mcp__basics__fetch",
                                      "ToolSearch"])

    async def test_probe_records_absent_and_notes(self):
        import chat_loop
        h = sm.LoopHandle(char_id="cass", scene="chat")
        c = _FakeClient()
        await chat_loop._probe_mcp(c, self._opts(), h)
        self.assertEqual(c.reconnects, ["ombre-brain"])
        absent = h.meta["absent_tools"]
        self.assertEqual(sorted(absent), ["basics", "ombre-brain"])
        self.assertEqual(absent["ombre-brain"]["tools"],
                         ["mcp__ombre-brain__hold", "mcp__ombre-brain__breath"])
        self.assertIn("still dead", absent["ombre-brain"]["error"])     # 异常原话
        self.assertEqual(absent["basics"]["tools"], ["mcp__basics__fetch"])   # 连上了但少工具
        self.assertNotIn("mail", absent)                                 # pending → connected 不算
        self.assertEqual(h.meta["absent_note_due"], ["basics", "ombre-brain"])
        # 下一轮注入前缀带告知，取一次即消费
        pre = chat_loop._turn_prefix(h)
        self.assertEqual(len(pre), 1)
        self.assertIn("〔记忆那边这会儿够不着", pre[0])
        self.assertIn("〔钟、闹铃", pre[0])
        self.assertEqual(chat_loop._turn_prefix(h), [])
        # 门：缺席工具拒、ToolSearch select: 缺席全名拒、在场的放行
        gate = chat_loop._wake_gate(h)

        def dec(out):
            return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision", "ask")
        d = await gate({"tool_name": "mcp__ombre-brain__hold", "tool_input": {}}, "t", None)
        self.assertEqual(dec(d), "deny")
        self.assertIn("记忆那边", d["hookSpecificOutput"]["permissionDecisionReason"])
        ts = await gate({"tool_name": "ToolSearch",
                         "tool_input": {"query": "select:mcp__mail__mail_read, mcp__ombre-brain__breath"}},
                        "t", None)
        self.assertEqual(dec(ts), "deny")
        ok = await gate({"tool_name": "ToolSearch",
                         "tool_input": {"query": "select:mcp__mail__mail_read"}}, "t", None)
        self.assertEqual(ok, {})
        self.assertEqual(await gate({"tool_name": "mcp__mail__mail_read", "tool_input": {}},
                                    "t", None), {})

    async def test_fake_client_without_status_is_skipped(self):
        import chat_loop
        h = sm.LoopHandle(char_id="cass", scene="chat")
        await chat_loop._probe_mcp(SimpleNamespace(), self._opts(), h)
        self.assertNotIn("absent_tools", h.meta)

    async def test_all_present_leaves_no_note(self):
        import chat_loop

        class AllGood:
            async def get_mcp_status(self):
                return {"mcpServers": [{"name": "mail", "status": "connected",
                                        "tools": [{"name": "mail_read"}]}]}
        h = sm.LoopHandle(char_id="cass", scene="chat")
        await chat_loop._probe_mcp(AllGood(), SimpleNamespace(tools=["mcp__mail__mail_read"]), h)
        self.assertEqual(h.meta["absent_tools"], {})
        self.assertNotIn("absent_note_due", h.meta)


class WakeCardTest(FlagsMixin, unittest.IsolatedAsyncioTestCase):
    """10-29 拍板：醒来轮也弹卡——门不按轮拒，can_use_tool 走醒来档超时、卡标 wake。"""

    def _handle(self, turn="wake"):
        h = sm.LoopHandle(char_id="cass", scene="chat")
        h.meta["turn_kind"] = turn
        h.meta["wake_tools"] = set()
        return h

    def test_write_route_asks_in_wake(self):
        import chat_loop
        h = self._handle()
        self.assertEqual(chat_loop._write_route(h, "Bash", {"command": "ls"}), ("ask", ""))
        self.assertEqual(chat_loop._write_route(
            h, "Edit", {"file_path": str(pipeline.CODE_ROOT / "README.md")}), ("ask", ""))

    async def test_gate_lets_question_card_through_in_wake(self):
        import chat_loop
        gate = chat_loop._wake_gate(self._handle())
        self.assertEqual(await gate({"tool_name": "AskUserQuestion",
                                     "tool_input": {"questions": []}}, "t", None), {})

    async def test_question_card_in_wake_uses_wake_timeout(self):
        import chat_loop
        from claude_agent_sdk import PermissionResultAllow
        seen: dict = {}

        async def fake_ask(char_id, **kw):
            seen.update(kw)
            return ({"q": "a"}, "")
        orig = questions.ask
        questions.ask = fake_ask
        try:
            out = await chat_loop._permit_gate(self._handle())(
                "AskUserQuestion", {"questions": [{"question": "q"}]},
                SimpleNamespace(title="", tool_use_id="q1"))
        finally:
            questions.ask = orig
        self.assertIsInstance(out, PermissionResultAllow)
        self.assertTrue(seen["wake"])
        self.assertEqual(seen["timeout"], questions.WAKE_TIMEOUT_SEC)
        self.assertEqual(out.updated_input["answers"], {"q": "a"})

    async def test_chat_turn_uses_chat_timeout(self):
        import chat_loop
        seen: dict = {}

        async def fake_ask(char_id, **kw):
            seen.update(kw)
            return (False, "不行")
        h = self._handle(turn="chat")
        h.meta["said_for"] = {"t1": "我动一下"}
        orig = permits.ask
        permits.ask = fake_ask
        try:
            await chat_loop._permit_gate(h)(
                "Bash", {"command": "ls"}, SimpleNamespace(title="", tool_use_id="t1"))
        finally:
            permits.ask = orig
        self.assertFalse(seen["wake"])
        self.assertEqual(seen["timeout"], permits.CHAT_TIMEOUT_SEC)


class YieldTest(unittest.IsolatedAsyncioTestCase):
    """TA 来了：醒来轮的卡自动拒（理由原话给模型）；聊天轮的卡不动。"""

    def setUp(self):
        import notify
        permits._pending.clear()
        questions._pending.clear()
        self._bark_orig = notify.bark_push
        notify.bark_push = lambda text, title=None: True

    def tearDown(self):
        import notify
        notify.bark_push = self._bark_orig
        permits._pending.clear()
        questions._pending.clear()

    async def test_permit_yield_only_wake(self):
        wake_t = asyncio.create_task(permits.ask("cass", tool="Bash", summary="ls",
                                                 timeout=5, wake=True))
        chat_t = asyncio.create_task(permits.ask("cass", tool="Bash", summary="ls",
                                                 timeout=5, wake=False))
        other_t = asyncio.create_task(permits.ask("default", tool="Bash", summary="ls",
                                                  timeout=5, wake=True))
        await asyncio.sleep(0.01)
        self.assertEqual(len(permits.pending()), 3)
        self.assertEqual(permits.yield_wake("cass", "TA 来了"), 1)
        self.assertEqual(await asyncio.wait_for(wake_t, 1), (False, "TA 来了"))
        await asyncio.sleep(0.01)
        self.assertEqual({r["char"] for r in permits.pending()}, {"cass", "default"})
        for t in (chat_t, other_t):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    async def test_question_yield(self):
        t = asyncio.create_task(questions.ask("cass", questions=[{"question": "q"}],
                                              timeout=5, wake=True))
        await asyncio.sleep(0.01)
        self.assertEqual(questions.yield_wake("cass", "TA 来了"), 1)
        self.assertEqual(await asyncio.wait_for(t, 1), (None, "TA 来了"))
        self.assertEqual(questions.yield_wake("cass", "TA 来了"), 0)

    async def test_stream_turn_entry_yields(self):
        """stream_turn / inject_pump_user 入口都调 _yield_wake_cards。"""
        import chat_loop
        t = asyncio.create_task(permits.ask("cass", tool="Bash", summary="ls",
                                            timeout=5, wake=True))
        await asyncio.sleep(0.01)
        chat_loop._yield_wake_cards("cass")
        self.assertEqual(await asyncio.wait_for(t, 1), (False, chat_loop.YIELD_MSG))


class BuildOptionsSlimTest(FlagsMixin, unittest.TestCase):
    """§3.3 第二刀：文件工具段 / 问答卡段不再拼进系统提示；one_turn_hint 真的在最末；
    工具照挂；Ombre 按配置挂（不探活）。"""

    def test_system_prompt_ends_with_one_turn_hint(self):
        import chat_loop
        import plugins
        tmp = Path(tempfile.mkdtemp())
        (tmp / "persona.md").write_text("人设", "utf-8")
        orig = (pipeline.rendered_persona, pipeline.tool_menu_block, pipeline.sticker_block,
                plugins.mounted, pipeline._pet_mcp_mounted, pipeline._skills_mounted,
                pipeline._prs_mounted, pipeline.ombre_configured, pipeline.ombre_alive,
                pipeline._basics_mcp_config, chat_loop._merge_mcp_file, chat_loop._game_capable,
                os.environ.pop("ANTHROPIC_API_KEY", None))
        probed = []
        pipeline.rendered_persona = lambda cid=None: tmp / "persona.md"
        pipeline.tool_menu_block = lambda ctx, cid=None: "■ 菜单"
        pipeline.sticker_block = lambda cat: ""
        plugins.mounted = lambda ctx, cid=None: (None, [])
        pipeline._pet_mcp_mounted = lambda cid=None: False
        pipeline._skills_mounted = lambda ctx, cid=None: False
        pipeline._prs_mounted = lambda cid=None: False
        pipeline.ombre_configured = lambda cid=None: True
        pipeline.ombre_alive = lambda cid=None: probed.append(1) or False
        pipeline._basics_mcp_config = lambda cid=None: tmp / "basics.json"
        chat_loop._merge_mcp_file = lambda servers, path: None
        chat_loop._game_capable = lambda cid: False
        try:
            h = sm.LoopHandle(char_id="cass", scene="chat")
            opts = chat_loop.build_options("cass", None, h)
        finally:
            (pipeline.rendered_persona, pipeline.tool_menu_block, pipeline.sticker_block,
             plugins.mounted, pipeline._pet_mcp_mounted, pipeline._skills_mounted,
             pipeline._prs_mounted, pipeline.ombre_configured, pipeline.ombre_alive,
             pipeline._basics_mcp_config, chat_loop._merge_mcp_file,
             chat_loop._game_capable) = orig[:12]
            if orig[12] is not None:
                os.environ["ANTHROPIC_API_KEY"] = orig[12]
        sp = opts.system_prompt
        self.assertTrue(sp.endswith(pipeline.one_turn_hint("chat_session")), sp[-200:])
        self.assertNotIn("只读的文件工具", sp)
        self.assertNotIn("出一张选择卡", sp)
        self.assertIn("■ 菜单", sp)
        for t in ("Read", "Edit", "Bash", "AskUserQuestion"):
            self.assertIn(t, opts.tools)
        self.assertNotIn("AskUserQuestion", opts.allowed_tools)
        self.assertIn("ombre-brain", opts.mcp_servers)      # 按配置挂
        self.assertEqual(probed, [])                        # 没探活


if __name__ == "__main__":
    unittest.main()
