"""PLAN_prs §4：写类按路径分路——本角色工作副本直接放行、别人的副本 / 副本里的 .git 拒、
工作副本以外 chat 轮弹卡 / wake 轮拒；Bash 醒来轮拒。PR 工具的挂载口径和系统提示两种措辞。
全部离线（路径不用真存在，resolve 只是算路径）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_prs_gate -v
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import permits
import pipeline
import prs


def _decision(out):
    return (out or {}).get("hookSpecificOutput", {}).get("permissionDecision", "ask")


class FlagsMixin:
    prs_on = True

    def setUp(self):
        self._orig = (config.WAKE_PRS_ENABLED, config.WRITE_TOOLS_ENABLED, config.CHAT_ENGINE)
        config.WAKE_PRS_ENABLED = self.prs_on
        config.WRITE_TOOLS_ENABLED = True
        config.CHAT_ENGINE = "sdk"
        permits._pending.clear()

    def tearDown(self):
        config.WAKE_PRS_ENABLED, config.WRITE_TOOLS_ENABLED, config.CHAT_ENGINE = self._orig
        permits._pending.clear()

    def handle(self, cid="default", turn=None):
        import session_mgr as sm
        h = sm.LoopHandle(char_id=cid, scene="chat")
        if turn:
            h.meta["turn_kind"] = turn
            h.meta["wake_tools"] = set()
        return h

    async def gate(self, tool, tool_input, cid="default", turn=None):
        import chat_loop
        return await chat_loop._wake_gate(self.handle(cid, turn))(
            {"tool_name": tool, "tool_input": tool_input}, "t", None)

    @staticmethod
    def wt(cid, rel="server/x.py"):
        return str(prs.REPO_ROOT / ".worktrees" / cid / rel)

    @staticmethod
    def main(rel="server/x.py"):
        return str(pipeline.CODE_ROOT / rel)


class GateOnTest(FlagsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_own_worktree_allowed_both_turns(self):
        for turn in (None, "wake"):
            for tool, key in (("Edit", "file_path"), ("Write", "file_path"),
                              ("NotebookEdit", "notebook_path")):
                out = await self.gate(tool, {key: self.wt("default")}, turn=turn)
                self.assertEqual(_decision(out), "allow", (turn, tool))

    async def test_relative_escape_from_worktree_is_main(self):
        p = self.wt("default", "../../server/x.py")        # 爬出副本回到主目录
        self.assertEqual(_decision(await self.gate("Edit", {"file_path": p}, turn="wake")),
                         "deny")

    async def test_other_worktree_denied(self):
        for other in ("cass", "_baseline"):
            out = await self.gate("Edit", {"file_path": self.wt(other)})
            self.assertEqual(_decision(out), "deny", other)
            self.assertIn("别人的工作副本",
                          out["hookSpecificOutput"]["permissionDecisionReason"])

    async def test_worktree_git_denied(self):
        for rel in (".git", ".git/config"):
            out = await self.gate("Write", {"file_path": self.wt("default", rel)})
            self.assertEqual(_decision(out), "deny", rel)

    async def test_main_dir_chat_asks_wake_denies(self):
        self.assertEqual(_decision(await self.gate("Edit", {"file_path": self.main()})),
                         "ask")
        out = await self.gate("Edit", {"file_path": self.main()}, turn="wake")
        self.assertEqual(_decision(out), "deny")
        self.assertIn("pr_start", out["hookSpecificOutput"]["permissionDecisionReason"])

    async def test_bash_chat_asks_wake_denies(self):
        self.assertEqual(_decision(await self.gate("Bash", {"command": "ls"})), "ask")
        out = await self.gate("Bash", {"command": "ls"}, turn="wake")
        self.assertEqual(_decision(out), "deny")
        self.assertIn("pr_test", out["hookSpecificOutput"]["permissionDecisionReason"])

    async def test_path_guard_still_first(self):
        out = await self.gate("Edit", {"file_path": self.wt("default", "server/.env")})
        self.assertEqual(_decision(out), "deny")


class PermitFallbackTest(FlagsMixin, unittest.IsolatedAsyncioTestCase):
    """门的 allow 万一没被 CLI 认、调用落进 can_use_tool：照样放行，不弹卡。"""

    async def cb(self, tool, tool_input, turn=None):
        import chat_loop
        return await chat_loop._permit_gate(self.handle(turn=turn))(
            tool, tool_input, SimpleNamespace(title="", tool_use_id="t9"))

    async def test_own_worktree_allowed_without_card(self):
        from claude_agent_sdk import PermissionResultAllow
        for turn in (None, "wake"):
            out = await self.cb("Edit", {"file_path": self.wt("default")}, turn)
            self.assertIsInstance(out, PermissionResultAllow)
        self.assertEqual(permits.pending(), [])

    async def test_wake_main_dir_denied_without_card(self):
        from claude_agent_sdk import PermissionResultDeny
        out = await self.cb("Write", {"file_path": self.main()}, "wake")
        self.assertIsInstance(out, PermissionResultDeny)
        self.assertIn("pr_start", out.message)
        self.assertEqual(permits.pending(), [])

    async def test_other_worktree_denied_in_chat(self):
        from claude_agent_sdk import PermissionResultDeny
        out = await self.cb("Edit", {"file_path": self.wt("cass")})
        self.assertIsInstance(out, PermissionResultDeny)
        self.assertEqual(permits.pending(), [])


class GateOffTest(FlagsMixin, unittest.IsolatedAsyncioTestCase):
    """WAKE_PRS 关着：工作副本不放行，醒来轮写类照样拒（不回退到弹卡，§9.6）。"""
    prs_on = False

    async def test_worktree_not_special_when_off(self):
        self.assertEqual(_decision(await self.gate("Edit", {"file_path": self.wt("default")})),
                         "ask")
        self.assertEqual(_decision(await self.gate("Edit", {"file_path": self.wt("default")},
                                                   turn="wake")), "deny")

    async def test_wake_reasons_dont_mention_pr_tools(self):
        for tool, inp in (("Edit", {"file_path": self.main()}), ("Bash", {"command": "ls"})):
            out = await self.gate(tool, inp, turn="wake")
            self.assertEqual(_decision(out), "deny")
            self.assertNotIn("pr_", out["hookSpecificOutput"]["permissionDecisionReason"])


class EventInjectionTest(FlagsMixin, unittest.TestCase):
    """§7：PR 事件进下一轮开头，取一次即消费；工具不在场时不取（留着）。"""

    def setUp(self):
        super().setUp()
        import shutil, tempfile
        self._dir, prs.PRS_DIR = prs.PRS_DIR, Path(tempfile.mkdtemp())
        self._cleanup = lambda: shutil.rmtree(prs.PRS_DIR, ignore_errors=True)

    def tearDown(self):
        self._cleanup()
        prs.PRS_DIR = self._dir
        super().tearDown()

    def test_chat_injection_carries_event_once(self):
        import chat_loop
        prs._event("default", "#3「x」已合并。")
        msgs = [SimpleNamespace(role="user", text="在吗", ts=None)]
        first = chat_loop.build_injection(msgs, "default")
        self.assertIn("【PR：#3「x」已合并。】", first)
        self.assertNotIn("PR：", chat_loop.build_injection(msgs, "default"))

    def test_wake_injection_carries_event(self):
        import wake_sdk
        prs._event("default", "#4「y」已合并。")
        self.assertIn("〔PR：#4「y」已合并。〕", wake_sdk.wake_injection("default", "idle"))

    def test_kept_when_tools_off(self):
        prs._event("default", "#5「z」已合并。")
        config.WAKE_PRS_ENABLED = False
        self.assertEqual(pipeline.prs_event_lines("default"), [])
        config.WAKE_PRS_ENABLED = True
        self.assertEqual(pipeline.prs_event_lines("default"), ["#5「z」已合并。"])


class MountTest(FlagsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._alive = pipeline.ombre_alive
        pipeline.ombre_alive = lambda *a, **k: False       # 别去探 Ombre（离线）

    def tearDown(self):
        pipeline.ombre_alive = self._alive
        super().tearDown()

    def test_mounted_only_with_all_three(self):
        self.assertTrue(set(pipeline.PRS_MCP_TOOLS)
                        <= set(pipeline.mounted_tool_names("wake", "default")))
        for flag, off in (("WAKE_PRS_ENABLED", False), ("WRITE_TOOLS_ENABLED", False),
                          ("CHAT_ENGINE", "p")):
            orig = getattr(config, flag)
            setattr(config, flag, off)
            try:
                self.assertFalse(pipeline._prs_mounted("default"), flag)
                self.assertFalse(set(pipeline.PRS_MCP_TOOLS)
                                 & set(pipeline.mounted_tool_names("wake", "default")), flag)
            finally:
                setattr(config, flag, orig)

    def test_prompt_two_ways_only_when_on(self):
        import chat_loop
        self.assertIn("pr_start", chat_loop._write_paths_text("default"))
        config.WAKE_PRS_ENABLED = False
        text = chat_loop._write_paths_text("default")
        self.assertNotIn("pr_", text)
        self.assertIn("醒着", text)


if __name__ == "__main__":
    unittest.main()
