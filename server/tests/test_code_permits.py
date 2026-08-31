"""code_permits（PLAN_sdk §5.3 PR14-c 写权限带外门）单测：申请/拍板/超时/收摊、
一角色一份状态、门集成（查记录不查对话）。全部离线，状态打到临时目录。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_code_permits -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import code_permits as cp
import notify
import state_store


class PermitBase(unittest.TestCase):
    def setUp(self):
        import activity_log
        self.al = activity_log
        self.tmp = Path(tempfile.mkdtemp(prefix="permit_test_"))
        self._root_orig = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        # 批准会开段账（PR14-d）——账本目录也打到临时地，不碰生产
        self._al_orig = (activity_log.PATH, activity_log.ACT_DIR,
                         activity_log.SEG_DIR, activity_log.SHOT_DIR,
                         activity_log.OPEN_PATH)
        activity_log.PATH = self.tmp / "activity_log.jsonl"
        activity_log.ACT_DIR = self.tmp / "activity"
        activity_log.SEG_DIR = activity_log.ACT_DIR / "segments"
        activity_log.SHOT_DIR = activity_log.ACT_DIR / "shots"
        activity_log.OPEN_PATH = activity_log.ACT_DIR / "open_segments.json"
        self.barks: list[str] = []
        self._bark_orig = notify.bark_push
        notify.bark_push = lambda text, title=None: self.barks.append(text) or True

    def tearDown(self):
        notify.bark_push = self._bark_orig
        state_store.CHAR_STATE_ROOT = self._root_orig
        (self.al.PATH, self.al.ACT_DIR, self.al.SEG_DIR, self.al.SHOT_DIR,
         self.al.OPEN_PATH) = self._al_orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _grant(self, cid="cass", reason="x") -> str:
        r = cp.request(cid, reason)
        cp.decide(cid, r["id"], True)
        return r["id"]


class PermitFlowTest(PermitBase):
    def test_request_grant_revoke(self):
        self.assertFalse(cp.active("cass"))
        r = cp.request("cass", "想改 server/x.py")
        self.assertEqual(r["state"], "pending")
        self.assertTrue(r["renewed"])
        self.assertEqual(len(self.barks), 1)
        self.assertIn("写代码", self.barks[0])
        # 已有待批单：不重推 Bark（注入面怂恿也刷不了屏）
        r2 = cp.request("cass", "再试一次")
        self.assertFalse(r2["renewed"])
        self.assertEqual(r2["id"], r["id"])
        self.assertEqual(len(self.barks), 1)
        # 拍板批准 → active；granted 期间再 request 直接回 granted
        self.assertTrue(cp.decide("cass", r["id"], True)["ok"])
        self.assertTrue(cp.active("cass"))
        self.assertEqual(cp.request("cass")["state"], "granted")
        # 收摊即失效
        cp.revoke("cass", "test")
        self.assertFalse(cp.active("cass"))
        self.assertIsNone(cp.status("cass")["pending"])

    def test_deny_clears_pending(self):
        r = cp.request("cass", "x")
        self.assertTrue(cp.decide("cass", r["id"], False)["ok"])
        self.assertFalse(cp.active("cass"))
        self.assertIsNone(cp.status("cass")["pending"])

    def test_wrong_or_expired_id_refused(self):
        self.assertFalse(cp.decide("cass", "nope", True)["ok"])
        r = cp.request("cass", "x")
        self.assertFalse(cp.decide("cass", "nope", True)["ok"])
        self.assertTrue(cp.active("cass") is False)
        # 单号对不上不许误伤真单
        self.assertTrue(cp.decide("cass", r["id"], True)["ok"])

    def test_timeout_auto_denies(self):
        cp.request("cass", "x")
        ttl_orig = cp.REQUEST_TTL_SEC
        cp.REQUEST_TTL_SEC = -1        # 立刻过期（惰性判：读状态时清）
        try:
            self.assertIsNone(cp.status("cass")["pending"])
            self.assertFalse(cp.active("cass"))
        finally:
            cp.REQUEST_TTL_SEC = ttl_orig

    def test_per_char_isolation(self):
        r = cp.request("cass", "x")
        cp.decide("cass", r["id"], True)
        self.assertTrue(cp.active("cass"))
        self.assertFalse(cp.active("default"))    # 串台纪律：批准不跨角色
        cp.revoke("default", "no-op")             # 幂等，不碰 cass 的
        self.assertTrue(cp.active("cass"))


class SegmentLifecycleTest(PermitBase):
    """PR14-d：场的边界跟着批准走——批准开段账、收摊关段账落区间行。"""

    def test_grant_opens_and_revoke_closes_segment(self):
        self._grant("cass")
        seg = cp.active_seg("cass")
        self.assertIsNotNone(seg)
        self.assertIn(seg, self.al.open_segments())
        cp.revoke("cass", "test")
        self.assertIsNone(cp.active_seg("cass"))
        self.assertEqual(self.al.open_segments(), {})
        ivs = self.al.read_intervals("cass", scene="code")
        self.assertEqual(len(ivs), 1)
        self.assertEqual(ivs[0]["note"], "写代码")

    def test_capsule_capture_needs_open_seg(self):
        import chat_loop
        chat_loop._capture_capsules("cass", "◆ 工具不过桥 ← forge.py:241")
        self.assertEqual(self.al.read_intervals("cass"), [])   # 没批=不落
        self._grant("cass")
        seg = cp.active_seg("cass")
        chat_loop._capture_capsules(
            "cass", "干完了。\n◆ 工具不过桥 ← forge.py:241\n◆ 判脏=发送前比对 ← chat_loop.py:122\n完事。")
        evs = self.al.read_events(seg)
        self.assertEqual([e["kind"] for e in evs], ["capsule", "capsule"])
        self.assertEqual(evs[0]["text"], "工具不过桥 ← forge.py:241")

    def test_addendum_block_carries_capsule_contract(self):
        import chat_loop
        blk = chat_loop._code_addendum_block()
        self.assertIn("◆", blk)
        self.assertIn("写权限批给你了", blk)
        self.assertNotIn("上机", blk)                   # 措辞纪律：能力不是场所


class WriteGateTest(PermitBase, unittest.IsolatedAsyncioTestCase):
    """门集成（chat_loop._wake_gate）：写类查带外记录；批了也过路径闸；
    电脑归属互斥（PR14-d）。"""

    def setUp(self):
        super().setUp()
        import plugins
        self._owner_orig = plugins.owner_of
        plugins.owner_of = lambda res: "cass"     # 电脑归 cass（测试不读生产插件态）

    def tearDown(self):
        import plugins
        plugins.owner_of = self._owner_orig
        super().tearDown()

    def _gate(self, cid="cass"):
        import chat_loop
        import session_mgr as sm
        handle = sm.LoopHandle(char_id=cid, scene="chat")
        return chat_loop._wake_gate(handle)

    async def test_write_denied_without_permit_and_requests(self):
        gate = self._gate()
        out = await gate({"tool_name": "Edit",
                          "tool_input": {"file_path": "/tmp/x.py"}}, "t", None)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("写权限", out["hookSpecificOutput"]["permissionDecisionReason"])
        # 拒的同时替他递了申请（申请即门口动作，批不批在 TA）
        st = cp.status("cass")
        self.assertIsNotNone(st["pending"])
        self.assertEqual(len(self.barks), 1)

    async def test_granted_allows_in_root_denies_outside(self):
        import pipeline
        r = cp.request("cass", "x")
        cp.decide("cass", r["id"], True)
        gate = self._gate()
        ok = await gate({"tool_name": "Edit",
                         "tool_input": {"file_path":
                                        str(pipeline.CODE_ROOT / "README.md")}},
                        "t", None)
        self.assertEqual(ok, {})
        bad = await gate({"tool_name": "Edit",
                          "tool_input": {"file_path": "/etc/hosts"}}, "t", None)
        self.assertEqual(bad["hookSpecificOutput"]["permissionDecision"], "deny")
        # Bash 没路径参数：granted 即放行（TA 拍板过的信任面）
        sh = await gate({"tool_name": "Bash",
                         "tool_input": {"command": "git status"}}, "t", None)
        self.assertEqual(sh, {})

    async def test_non_owner_denied_without_request(self):
        """computer 互斥：电脑不归他 → 拒且不递申请（递了批了也是串台面）。"""
        gate = self._gate("default")
        out = await gate({"tool_name": "Edit",
                          "tool_input": {"file_path": "/tmp/x.py"}}, "t", None)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("归", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(cp.status("default")["pending"])
        self.assertEqual(self.barks, [])


if __name__ == "__main__":
    unittest.main()
