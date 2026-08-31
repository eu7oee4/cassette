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
        self.tmp = Path(tempfile.mkdtemp(prefix="permit_test_"))
        self._root_orig = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        self.barks: list[str] = []
        self._bark_orig = notify.bark_push
        notify.bark_push = lambda text, title=None: self.barks.append(text) or True

    def tearDown(self):
        notify.bark_push = self._bark_orig
        state_store.CHAR_STATE_ROOT = self._root_orig
        shutil.rmtree(self.tmp, ignore_errors=True)


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


class WriteGateTest(PermitBase, unittest.IsolatedAsyncioTestCase):
    """门集成（chat_loop._wake_gate）：写类查带外记录；批了也过路径闸。"""

    def _gate(self):
        import chat_loop
        import session_mgr as sm
        handle = sm.LoopHandle(char_id="cass", scene="chat")
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


if __name__ == "__main__":
    unittest.main()
