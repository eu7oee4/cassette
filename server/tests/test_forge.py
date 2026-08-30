"""forge（PLAN_sdk S0/PR2）单测：确定性、uuid 链、slug、覆盖写、窗口裁剪。全部离线，
不碰真 ~/.claude（projects_root 指临时目录）。真机回归另走 tools/forge_regress.py。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_forge -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forge

MSGS = [
    {"role": "user", "text": "你选一个暗号告诉我", "ts": 1700000000},
    {"role": "assistant", "text": "我选 VELVET，我自己挑的。"},
    {"role": "user", "text": "为什么选它？", "ts": 1700000100},
    {"role": "assistant", "text": "念起来软软的。", "ts": 1700000105},
]
CWD = "/Users/x/proj"


class ForgeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _render(self, msgs=MSGS, root=None, **kw):
        sid = forge.render(msgs, cwd=CWD, projects_root=root or self.root, **kw)
        return sid, forge.transcript_path(CWD, sid, root or self.root)

    def _events(self, path):
        return [json.loads(l) for l in path.read_text().splitlines()]

    def test_deterministic(self):
        """同入同出：两次铸造（不同根目录）字节相同、session_id 相同。"""
        sid1, p1 = self._render()
        with tempfile.TemporaryDirectory() as d2:
            sid2, p2 = self._render(root=Path(d2))
            self.assertEqual(sid1, sid2)
            self.assertEqual(p1.read_bytes(), p2.read_bytes())

    def test_uuid_chain_and_fidelity(self):
        sid, path = self._render()
        evs = self._events(path)
        self.assertEqual(len(evs), len(MSGS))          # 一条消息一个事件，不多不少
        self.assertIsNone(evs[0]["parentUuid"])
        for prev, cur in zip(evs, evs[1:]):
            self.assertEqual(cur["parentUuid"], prev["uuid"])   # 链不断
        for ev, m in zip(evs, MSGS):
            self.assertEqual(ev["sessionId"], sid)
            self.assertEqual(ev["type"], m["role"])
            # 红线：字面严格等于权威原句
            self.assertEqual(ev["message"]["content"][0]["text"], m["text"])

    def test_consecutive_same_role_merged(self):
        """连续同角色合并成一轮（08-30 game 污染根修）：铸出来的历史必须严格
        user/assistant 交替——几十个连续 assistant 事件会让 CLI 在缝里塞合成
        user 槽，模型学舌把「user·system<total_tokens>…」缀在话尾。"""
        msgs = [
            {"role": "user", "text": "去读两章", "ts": 100},
            {"role": "assistant", "text": "好"},
            {"role": "assistant", "text": "这句妙"},
            {"role": "assistant", "text": "读完了"},
            {"role": "user", "text": "怎么样？", "ts": 200},
            {"role": "user", "text": "喂？", "ts": 201},
            {"role": "assistant", "text": "在想", "ts": 205},
        ]
        _, path = self._render(msgs=msgs)
        evs = self._events(path)
        self.assertEqual([e["type"] for e in evs], ["user", "assistant",
                                                    "user", "assistant"])
        # 字面=逐字拼接（\n\n），一个字不动
        self.assertEqual(evs[1]["message"]["content"][0]["text"],
                         "好\n\n这句妙\n\n读完了")
        self.assertEqual(evs[2]["message"]["content"][0]["text"],
                         "怎么样？\n\n喂？")
        # 合并轮的时间戳=首条的（时间是权威源的事实）
        self.assertTrue(evs[2]["timestamp"].startswith("1970-01-01T00:03:20"))

    def test_assistant_event_shape(self):
        _, path = self._render()
        ev = self._events(path)[1]
        msg = ev["message"]
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["stop_reason"], "end_turn")
        self.assertIn("usage", msg)
        self.assertTrue(ev["requestId"].startswith("req_forge_"))

    def test_first_ts_required(self):
        with self.assertRaises(ValueError):
            self._render([{"role": "user", "text": "hi"}])

    def test_bad_role_and_empty_text(self):
        with self.assertRaises(ValueError):
            self._render([{"role": "system", "text": "x", "ts": 1}])
        with self.assertRaises(ValueError):
            self._render([{"role": "user", "text": "  ", "ts": 1}])

    def test_overwrite_not_append(self):
        """同 session_id 重铸 = 整个覆盖（权威盖掉一切），绝不追加。"""
        sid, path = self._render(session_id="11111111-2222-4333-8444-555555555555")
        self.assertEqual(len(self._events(path)), 4)
        self._render(MSGS[:2], session_id=sid)
        self.assertEqual(len(self._events(path)), 2)

    def test_permissions(self):
        _, path = self._render()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_slug(self):
        self.assertEqual(forge.slug("/Users/x/claude_cwd.v2"), "-Users-x-claude-cwd-v2")

    def test_slug_collision(self):
        forge.assert_no_slug_collision(["/a/b", "/a/c"])
        forge.assert_no_slug_collision(["/a/b", "/a/b"])      # 同一 cwd 不算撞
        with self.assertRaises(ValueError):
            forge.assert_no_slug_collision(["/a/b_c", "/a/b-c"])

    def test_ops_check(self):
        """PR3 运维自检：权限松了要报、fix 能收紧、干净时零问题。TM 那条不进单测。"""
        sid, path = self._render()
        pdir = path.parent
        self.assertEqual(forge.ops_check(root=self.root, check_tm=False), [])
        pdir.chmod(0o755)
        path.chmod(0o644)
        probs = forge.ops_check(root=self.root, check_tm=False)
        self.assertEqual(len(probs), 1)
        self.assertIn("2 个路径", probs[0])
        forge.ops_check(root=self.root, check_tm=False, fix=True)
        self.assertEqual(forge.ops_check(root=self.root, check_tm=False), [])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_ops_check_sync_marker(self):
        bad = self.root / "Dropbox" / "projects"
        bad.mkdir(parents=True)
        probs = forge.ops_check(root=bad, check_tm=False)
        self.assertTrue(any("同步盘" in p for p in probs))

    def test_tail_window(self):
        msgs = []
        for i in range(10):
            msgs.append({"role": "user", "text": f"问 {i}", "ts": i})
            msgs.append({"role": "assistant", "text": f"答 {i}", "ts": i})
        w = forge.tail_window(msgs, keep_turns=2)
        self.assertEqual(len(w), 4)
        self.assertEqual(w[0], {"role": "user", "text": "问 8", "ts": 8})
        # token 上限：把额度掐到只够尾巴几条，窗口应从 user 轮开头
        w2 = forge.tail_window(msgs, keep_tokens=6, keep_turns=99)
        self.assertLess(len(w2), len(msgs))
        self.assertEqual(w2[0]["role"], "user")


if __name__ == "__main__":
    unittest.main()
