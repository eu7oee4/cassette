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


class TimeoutPolicyTest(unittest.TestCase):
    """§1.3：超时分轮来源——聊天轮机主大概率在场给长些，wake 轮快拒。"""

    def test_wake_faster_than_chat(self):
        self.assertLess(permits.WAKE_TIMEOUT_SEC, permits.CHAT_TIMEOUT_SEC)
        self.assertEqual(permits.timeout_for("wake"), permits.WAKE_TIMEOUT_SEC)
        self.assertEqual(permits.timeout_for("chat"), permits.CHAT_TIMEOUT_SEC)
        self.assertEqual(permits.timeout_for(""), permits.CHAT_TIMEOUT_SEC)


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

    async def test_wake_turn_write_also_reaches_ask(self):
        """§1.3：wake 轮也能申请（快拒档在超时里，不在 hook）——写类分支
        先于醒来禁用面。"""
        gate = self._gate(turn="wake")
        self.assertEqual(await gate({"tool_name": "Bash",
                                     "tool_input": {"command": "ls"}},
                                    "t", None), {})

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
        return chat_loop._permit_gate(handle)

    async def test_allow_then_deny_with_reason(self):
        from claude_agent_sdk import (PermissionResultAllow,
                                      PermissionResultDeny)
        cb = self._cb()
        ctx = SimpleNamespace(title="Claude wants to edit x.py",
                              tool_use_id="toolu_9")
        task = asyncio.ensure_future(cb("Edit", {"file_path": "x.py"}, ctx))
        await asyncio.sleep(0)
        card = permits.pending()[0]
        self.assertEqual(card["id"], "toolu_9")        # 单号=那次调用本身
        self.assertEqual(card["title"], "Claude wants to edit x.py")
        permits.decide("toolu_9", True)
        self.assertIsInstance(await task, PermissionResultAllow)

        task = asyncio.ensure_future(cb("Edit", {"file_path": "x.py"}, ctx))
        await asyncio.sleep(0)
        permits.decide("toolu_9", False, "先不动它")
        out = await task
        self.assertIsInstance(out, PermissionResultDeny)
        self.assertIn("先不动它", out.message)
        self.assertFalse(out.interrupt)                # 常规拒不掐轮

    async def test_timeout_denies_with_message(self):
        from claude_agent_sdk import PermissionResultDeny
        orig = permits.CHAT_TIMEOUT_SEC
        permits.CHAT_TIMEOUT_SEC = 0.01
        try:
            cb = self._cb()
            out = await cb("Bash", {"command": "ls"},
                           SimpleNamespace(title="", tool_use_id="t0"))
            self.assertIsInstance(out, PermissionResultDeny)
            self.assertTrue(out.message)               # 有交代，不是空拒
        finally:
            permits.CHAT_TIMEOUT_SEC = orig

    async def test_wake_turn_uses_wake_timeout(self):
        from claude_agent_sdk import PermissionResultDeny
        orig = permits.WAKE_TIMEOUT_SEC
        permits.WAKE_TIMEOUT_SEC = 0.01
        try:
            cb = self._cb(turn="wake")
            out = await cb("Bash", {"command": "ls"},
                           SimpleNamespace(title="", tool_use_id="t1"))
            self.assertIsInstance(out, PermissionResultDeny)
        finally:
            permits.WAKE_TIMEOUT_SEC = orig


class CapsuleTest(unittest.TestCase):
    """§4：capsule 的家＝行为账（code 无场之后事件账没有段地址，按角色一本的
    行为账接住「◆ 结论 ← 出处」；PLAN_native §4 记了一笔）。"""

    def setUp(self):
        import activity_log
        self.al = activity_log
        self.tmp = Path(tempfile.mkdtemp(prefix="capsule_test_"))
        self._al_orig = (activity_log.PATH, activity_log.ACT_DIR,
                         activity_log.SEG_DIR, activity_log.SHOT_DIR,
                         activity_log.OPEN_PATH)
        activity_log.PATH = self.tmp / "activity_log.jsonl"
        activity_log.ACT_DIR = self.tmp / "activity"
        activity_log.SEG_DIR = activity_log.ACT_DIR / "segments"
        activity_log.SHOT_DIR = activity_log.ACT_DIR / "shots"
        activity_log.OPEN_PATH = activity_log.ACT_DIR / "open_segments.json"

    def tearDown(self):
        (self.al.PATH, self.al.ACT_DIR, self.al.SEG_DIR, self.al.SHOT_DIR,
         self.al.OPEN_PATH) = self._al_orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_capsule_lands_in_acts(self):
        import chat_loop
        chat_loop._capture_capsules(
            "cass", "干完了。\n◆ 工具不过桥 ← forge.py:241\n"
                    "◆ 判脏=发送前比对 ← chat_loop.py:122\n完事。")
        acts = self.al.recent_acts("cass")
        self.assertEqual([a["tool"] for a in acts], ["capsule", "capsule"])
        self.assertEqual(acts[0]["text"], "工具不过桥 ← forge.py:241")
        self.assertEqual(acts[0]["scene"], "chat")

    def test_wake_turn_tagged(self):
        import chat_loop
        chat_loop._capture_capsules("cass", "◆ 结论 ← x.py:1", turn="wake")
        self.assertEqual(self.al.recent_acts("cass")[0]["scene"], "wake")

    def test_no_marker_no_row(self):
        import chat_loop
        chat_loop._capture_capsules("cass", "就聊聊天，没干活。")
        self.assertEqual(self.al.recent_acts("cass"), [])


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

    def test_readonly_does_not_emit(self):
        out = self._payload({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r", "name": "Read",
             "input": {"file_path": "a.py"}},
            {"type": "text", "text": "看一眼"},
        ]}})
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
