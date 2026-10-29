"""permits（PLAN_native §1）：写类调用原地审批。五条主线——挂起/批准/拒绝/
超时/后端重启作废；外加门集成（hook 只过路径闸，写类落 ask → can_use_tool）、
回调两形（Allow/Deny）、capsule 落行为账。全部离线。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_permits -v
"""
import asyncio
import importlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notify
import permits
import state_store


class PermitBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.barks: list[str] = []
        self._bark_orig = notify.bark_push
        notify.bark_push = lambda text, title=None: self.barks.append(text) or True
        permits._pending.clear()

    def tearDown(self):
        notify.bark_push = self._bark_orig
        permits._pending.clear()


class AskDecideTest(PermitBase):
    """挂起 → 卡在册 + Bark；批准/拒绝原地返回；超时自动拒；卡拍完即清。"""

    async def test_hang_registers_card_then_approve(self):
        task = asyncio.ensure_future(permits.ask(
            "cass", tool="Bash", summary="git status",
            title="Claude wants to run git status", permit_id="toolu_01"))
        await asyncio.sleep(0)
        cards = permits.pending()
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual((c["id"], c["char"], c["tool"]),
                         ("toolu_01", "cass", "Bash"))
        self.assertEqual(c["summary"], "git status")
        self.assertNotIn("future", c)                  # REST 面不带进程内脏器
        self.assertTrue(permits.waiting("cass"))
        self.assertFalse(permits.waiting("default"))
        self.assertEqual(len(self.barks), 1)
        self.assertIn("git status", self.barks[0])
        r = permits.decide("toolu_01", True)
        self.assertTrue(r["ok"])
        self.assertEqual(await task, (True, ""))
        self.assertEqual(permits.pending(), [])        # 卡拍完即清
        self.assertFalse(permits.waiting("cass"))

    async def test_deny_carries_reason(self):
        task = asyncio.ensure_future(
            permits.ask("cass", tool="Edit", summary="server/x.py"))
        await asyncio.sleep(0)
        pid = permits.pending()[0]["id"]
        permits.decide(pid, False, "这个文件先别动")
        self.assertEqual(await task, (False, "这个文件先别动"))

    async def test_timeout_auto_denies_and_cleans(self):
        got = await permits.ask("cass", tool="Bash", summary="ls",
                                timeout=0.01)
        self.assertIsNone(got)
        self.assertEqual(permits.pending(), [])
        self.assertFalse(permits.waiting("cass"))

    async def test_two_chars_two_signed_cards(self):
        """§2：两边同时想动手就是两张署名的卡——机主本人是串行化点，
        没有互斥、批谁先批谁都行。"""
        t1 = asyncio.ensure_future(permits.ask(
            "cass", tool="Edit", summary="a.py", permit_id="t1"))
        t2 = asyncio.ensure_future(permits.ask(
            "default", tool="Bash", summary="pytest", permit_id="t2"))
        await asyncio.sleep(0)
        self.assertEqual([c["char"] for c in permits.pending()],
                         ["cass", "default"])
        self.assertEqual([c["char"] for c in permits.pending("cass")], ["cass"])
        permits.decide("t2", False)                    # 后递的先批也行
        permits.decide("t1", True)
        self.assertEqual(await t1, (True, ""))
        self.assertEqual((await t2)[0], False)

    async def test_decide_unknown_id_loud(self):
        r = permits.decide("nope", True)
        self.assertFalse(r["ok"])
        self.assertIn("待批单", r["error"])

    async def test_restart_voids_pending(self):
        """内存态是特性（§1.2）：后端重启即作废，没有悬单要收拾。"""
        task = asyncio.ensure_future(permits.ask(
            "cass", tool="Bash", summary="x", permit_id="t"))
        await asyncio.sleep(0)
        fresh = importlib.reload(permits)              # 重启＝内存清零
        r = fresh.decide("t", True)
        self.assertFalse(r["ok"])
        self.assertEqual(fresh.pending(), [])
        task.cancel()                                  # 旧 future 随进程一起消失
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_nothing_touches_disk(self):
        """裁决没有持久状态可管——整个申请/拍板周期不落一个文件。"""
        tmp = Path(tempfile.mkdtemp(prefix="permit_disk_"))
        orig = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = tmp / "chars"
        try:
            task = asyncio.ensure_future(permits.ask(
                "cass", tool="Edit", summary="x.py", permit_id="d1"))
            await asyncio.sleep(0)
            permits.decide("d1", True)
            await task
            # display_name 认人时会 mkdir 角色目录（与裁决无关）；裁决本身
            # 一个文件都不许落——重启作废靠的就是这个。
            files = [p for p in tmp.rglob("*") if p.is_file()]
            self.assertEqual(files, [])
        finally:
            state_store.CHAR_STATE_ROOT = orig
            shutil.rmtree(tmp, ignore_errors=True)


class WriteGateTest(unittest.IsolatedAsyncioTestCase):
    """PreToolUse 门的写类分支（§1.4）：只过路径闸，别的判断一概不做——
    放过（{}）就落进 ask → can_use_tool。没有互斥、没有带外记录可查。"""

    def _gate(self, cid="cass", turn=None):
        import chat_loop
        import session_mgr as sm
        handle = sm.LoopHandle(char_id=cid, scene="chat")
        if turn:
            handle.meta["turn_kind"] = turn
            handle.meta["wake_tools"] = set()
        return chat_loop._wake_gate(handle)

    async def test_write_passes_hook_in_root(self):
        import pipeline
        gate = self._gate()
        ok = await gate({"tool_name": "Edit",
                         "tool_input": {"file_path":
                                        str(pipeline.CODE_ROOT / "README.md")}},
                        "t", None)
        self.assertEqual(ok, {})

    async def test_write_path_guard_still_bites(self):
        gate = self._gate()
        for bad_path in ("/etc/hosts", "../characters/default/char.json"):
            out = await gate({"tool_name": "Edit",
                              "tool_input": {"file_path": bad_path}}, "t", None)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"],
                             "deny", bad_path)

    async def test_bash_passes_without_path(self):
        """Bash 没路径参数，闸对它放空——机主看得到命令原文，拍板本身就是闸。"""
        gate = self._gate()
        self.assertEqual(await gate({"tool_name": "Bash",
                                     "tool_input": {"command": "git status"}},
                                    "t", None), {})

    async def test_wake_turn_bash_asks(self):
        """10-29：醒来轮也弹卡（原 PLAN_prs §4「醒来轮没有卡、Bash 在门里就拒」退役）。"""
        gate = self._gate(turn="wake")
        out = await gate({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                         "t", None)
        self.assertEqual(out, {})

    async def test_other_char_can_also_ask(self):
        """互斥退役（§2）：谁都能申请写，hook 不看资源归属。"""
        gate = self._gate(cid="default")
        self.assertEqual(await gate({"tool_name": "Bash",
                                     "tool_input": {"command": "ls"}},
                                    "t", None), {})


class PermitCallbackTest(PermitBase):
    """chat_loop._permit_gate（§1.2 的 SDK 回调面）：批→Allow 原地继续；
    拒→Deny 带机主理由；超时→Deny 有交代。"""

    def _cb(self, cid="cass", turn=None):
        import chat_loop
        import session_mgr as sm
        handle = sm.LoopHandle(char_id=cid, scene="chat")
        if turn:
            handle.meta["turn_kind"] = turn
        self.h = handle
        return chat_loop._permit_gate(handle)

    def said(self, tool_use_id, text):
        """消费端读到这次调用时认领的人话（chat_loop._said_feed 干的事）。"""
        self.h.meta.setdefault("said_for", {})[tool_use_id] = text

    async def test_allow_then_deny_with_reason(self):
        from claude_agent_sdk import (PermissionResultAllow,
                                      PermissionResultDeny)
        cb = self._cb()
        ctx = SimpleNamespace(title="Claude wants to edit x.py",
                              tool_use_id="toolu_9")
        self.said("toolu_9", "我把 x.py 里那个默认值改成 2，你那边的提醒会晚一分钟。")
        task = asyncio.ensure_future(cb("Edit", {"file_path": "x.py"}, ctx))
        await asyncio.sleep(0)
        card = permits.pending()[0]
        self.assertEqual(card["id"], "toolu_9")        # 单号=那次调用本身
        self.assertEqual(card["title"], "Claude wants to edit x.py")
        self.assertIn("晚一分钟", card["said"])         # §4.2：卡上印他动手前说的话
        permits.decide("toolu_9", True)
        self.assertIsInstance(await task, PermissionResultAllow)

        self.said("toolu_9", "再改一次。")
        task = asyncio.ensure_future(cb("Edit", {"file_path": "x.py"}, ctx))
        await asyncio.sleep(0)
        permits.decide("toolu_9", False, "先不动它")
        out = await task
        self.assertIsInstance(out, PermissionResultDeny)
        self.assertIn("先不动它", out.message)
        self.assertFalse(out.interrupt)                # 常规拒不掐轮

    async def test_no_words_since_last_card_denied_without_card(self):
        """§4.2：上一张卡之后没再说过话 → 拒，原话教他先说，不挂卡。"""
        from claude_agent_sdk import PermissionResultDeny
        cb = self._cb()
        self.said("t_quiet", "")
        out = await cb("Edit", {"file_path": "x.py"},
                       SimpleNamespace(title="", tool_use_id="t_quiet"))
        self.assertIsInstance(out, PermissionResultDeny)
        self.assertIn("印在卡上", out.message)
        self.assertEqual(permits.pending(), [])

    async def test_consumer_lagging_still_asks(self):
        """消费端没读到这次调用（等不到）：不拒，卡照弹，只是没有人话。"""
        import chat_loop
        orig = chat_loop.SAID_WAIT_SEC
        chat_loop.SAID_WAIT_SEC = 0.05
        try:
            cb = self._cb()
            task = asyncio.ensure_future(cb("Bash", {"command": "ls", "description": "列目录"},
                                            SimpleNamespace(title="", tool_use_id="t_lag")))
            await asyncio.sleep(0.2)
            card = permits.pending()[0]
            self.assertEqual((card["said"], card["note"]), ("", "列目录"))
            permits.decide("t_lag", False)
            await task
        finally:
            chat_loop.SAID_WAIT_SEC = orig

    async def test_timeout_denies_with_message(self):
        from claude_agent_sdk import PermissionResultDeny
        orig = permits.CHAT_TIMEOUT_SEC
        permits.CHAT_TIMEOUT_SEC = 0.01
        try:
            cb = self._cb()
            self.said("t0", "看一眼目录。")
            out = await cb("Bash", {"command": "ls"},
                           SimpleNamespace(title="", tool_use_id="t0"))
            self.assertIsInstance(out, PermissionResultDeny)
            self.assertTrue(out.message)               # 有交代，不是空拒
        finally:
            permits.CHAT_TIMEOUT_SEC = orig

    async def test_wake_turn_pops_card_and_yields_to_chat(self):
        """10-29 改口（原「醒来轮直接拒，不挂卡」退役）：醒来轮照弹卡、卡标 wake、
        超时走醒来档；TA 的消息一到（chat_loop._yield_wake_cards）卡自动拒让路。"""
        import asyncio
        import chat_loop
        from claude_agent_sdk import PermissionResultDeny
        cb = self._cb(turn="wake")
        self.said("t1", "我看一眼目录")
        task = asyncio.ensure_future(cb("Bash", {"command": "ls"},
                                        SimpleNamespace(title="", tool_use_id="t1")))
        await asyncio.sleep(0.01)
        cards = permits.pending()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["said"], "我看一眼目录")
        self.assertAlmostEqual(cards[0]["deadline"] - cards[0]["ts"],
                               permits.WAKE_TIMEOUT_SEC, delta=2)
        chat_loop._yield_wake_cards("cass")
        out = await asyncio.wait_for(task, 1)
        self.assertIsInstance(out, PermissionResultDeny)
        self.assertIn(chat_loop.YIELD_MSG, out.message)
        self.assertEqual(permits.pending(), [])


class SaidFeedTest(unittest.TestCase):
    """chat_loop._said_feed（§4.2）：按块顺序记「最近说的那段」，要弹卡的写类认领走。"""

    def setUp(self):
        import session_mgr as sm
        self.h = sm.LoopHandle(char_id="cass", scene="chat")
        self.h.meta["turn_kind"] = "chat"

    def feed(self, *blocks):
        import chat_loop
        from claude_agent_sdk import AssistantMessage
        return chat_loop._said_feed(self.h, AssistantMessage(content=list(blocks),
                                                             model="m"))

    def test_claim_then_empty(self):
        import pipeline
        from claude_agent_sdk import TextBlock, ToolUseBlock
        main = str(pipeline.CODE_ROOT / "README.md")
        out = self.feed(TextBlock(text="我改一下 README 的安装步骤。[[next:30]]"),
                        ToolUseBlock(id="e1", name="Edit", input={"file_path": main}),
                        ToolUseBlock(id="e2", name="Edit", input={"file_path": main}))
        self.assertEqual(out, {"e1": "我改一下 README 的安装步骤。", "e2": ""})
        self.assertEqual(self.h.meta["said_for"], out)
        # 新说一句 → 下一张卡又有话了；Read 不认领
        out = self.feed(TextBlock(text="再跑一下测试。"),
                        ToolUseBlock(id="r", name="Read", input={"file_path": main}),
                        ToolUseBlock(id="b1", name="Bash", input={"command": "ls"}))
        self.assertEqual(out, {"b1": "再跑一下测试。"})

    def test_own_worktree_and_denied_paths_have_no_card(self):
        import prs
        from claude_agent_sdk import TextBlock, ToolUseBlock
        wt = str(prs.worktree_path("cass") / "server" / "x.py")
        out = self.feed(TextBlock(text="在副本里改。"),
                        ToolUseBlock(id="w", name="Edit", input={"file_path": wt}),
                        ToolUseBlock(id="d", name="Edit", input={"file_path": "/etc/hosts"}))
        self.assertEqual(out, {})
        self.assertEqual(self.h.meta["said_buf"], "在副本里改。")   # 没被白白认领掉

    def test_wake_turn_claims_too(self):
        """10-29：醒来轮也弹卡，人话照样认领（原「醒来轮没有卡」退役）。"""
        import pipeline
        from claude_agent_sdk import TextBlock, ToolUseBlock
        self.h.meta["turn_kind"] = "wake"
        self.assertEqual(self.feed(TextBlock(text="x"), ToolUseBlock(
            id="e", name="Edit", input={"file_path": str(pipeline.CODE_ROOT / "a")})),
            {"e": "x"})


class SsePermitEventTest(unittest.TestCase):
    """sse._permit_events：写类 tool_use → 一条 permit SSE（单号=tool_use_id，
    带参数原文）；只读工具不触发。幽灵卡（路径闸拒）由 app 轮询收走，不在此测。"""

    @staticmethod
    def _payload(ev):
        import json
        import sse
        chunks = list(sse._permit_events(ev))
        return [json.loads(c.decode("utf-8")[len("data: "):]) for c in chunks]

    def test_edit_card_carries_diff(self):
        out = self._payload({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_p1", "name": "Edit",
             "input": {"file_path": "server/x.py",
                       "old_string": "a = 1", "new_string": "a = 2"}},
        ]}})
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual((p["type"], p["id"], p["tool"]),
                         ("permit", "toolu_p1", "Edit"))
        self.assertEqual(p["summary"], "server/x.py")
        self.assertIn("a = 1", p["detail"])
        self.assertIn("a = 2", p["detail"])
        self.assertGreater(p["deadline"], 0)

    def test_bash_card_shows_command(self):
        out = self._payload({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "p2", "name": "Bash",
             "input": {"command": "git add x.py\ngit commit -m ok"}},
        ]}})
        self.assertEqual(out[0]["summary"], "git add x.py")
        self.assertIn("git commit -m ok", out[0]["detail"])

    def test_said_map_filters_and_prints_words(self):
        # §4.2：有 permit_said 表时只推表里有人话的；工作副本的写 / 没说话的都不推（以前是幽灵卡）
        out = self._payload({"type": "assistant", "permit_said": {"a": "改个默认值。", "b": ""},
                             "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash",
             "input": {"command": "sed -i x", "description": "改默认值"}},
            {"type": "tool_use", "id": "b", "name": "Edit", "input": {"file_path": "x"}},
            {"type": "tool_use", "id": "c", "name": "Edit", "input": {"file_path": "wt"}},
        ]}})
        self.assertEqual([(p["id"], p["said"], p["note"]) for p in out],
                         [("a", "改个默认值。", "改默认值")])

    def test_readonly_does_not_emit(self):
        out = self._payload({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r", "name": "Read",
             "input": {"file_path": "a.py"}},
            {"type": "text", "text": "看一眼"},
        ]}})
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
