"""activity_log 事件账本（PLAN_sdk PR13）单测：开/记/收一场、截图引用、行为账、
30 天清理。全部离线，路径打到临时目录。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_activity_log -v
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import activity_log as al


class ActivityLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = (al.PATH, al.ACT_DIR, al.SEG_DIR, al.SHOT_DIR, al.OPEN_PATH)
        al.PATH = root / "activity_log.jsonl"
        al.ACT_DIR = root / "activity"
        al.SEG_DIR = al.ACT_DIR / "segments"
        al.SHOT_DIR = al.ACT_DIR / "shots"
        al.OPEN_PATH = al.ACT_DIR / "open_segments.json"

    def tearDown(self):
        (al.PATH, al.ACT_DIR, al.SEG_DIR, al.SHOT_DIR, al.OPEN_PATH) = self._orig
        self.tmp.cleanup()

    def test_segment_roundtrip(self):
        """开→记→收：seg_id 带身份、事件按序、收摊落区间行（格式=PR11 原样）。"""
        seg = al.open_segment("cass", "game", 1700000000)
        self.assertIn("cass", seg)
        self.assertIn(seg, al.open_segments())
        al.append_event(seg, "comment", text="这句妙")
        al.append_event(seg, "tool", tool="game_tap", ok=True)
        evs = al.read_events(seg)
        self.assertEqual([e["kind"] for e in evs], ["comment", "tool"])
        al.close_segment(seg, note="《如鸢》剧情")
        self.assertEqual(al.open_segments(), {})
        ivs = al.read_intervals("cass")
        self.assertEqual(len(ivs), 1)
        self.assertEqual(ivs[0]["start"], 1700000000)
        self.assertEqual(ivs[0]["note"], "《如鸢》剧情")

    def test_interval_seg_id_matches_open_segment(self):
        """区间行 → 事件账地址必须和 open_segment 同源（S3 补线③的找回路径）。"""
        seg = al.open_segment("cass", "game", 1700000000)
        self.assertEqual(al.interval_seg_id("cass", "game", 1700000000), seg)

    def test_event_text_cap(self):
        seg = al.open_segment("cass", "game")
        al.append_event(seg, "comment", text="喵" * 5000)
        self.assertEqual(len(al.read_events(seg)[0]["text"]), al.EVENT_TEXT_CAP)

    def test_shots_and_recent_refs(self):
        seg = al.open_segment("default", "game")
        refs = [al.save_shot(seg, b"jpg%d" % i) for i in range(4)]
        self.assertTrue(all(refs))
        self.assertEqual(al.recent_shot_refs(seg, 2), refs[-2:])
        Path(refs[-1]).unlink()          # 盘上没了的引用不回填
        self.assertEqual(al.recent_shot_refs(seg, 2), refs[1:3])
        self.assertEqual(Path(refs[0]).stat().st_mode & 0o777, 0o600)

    def test_acts(self):
        al.append_act("cass", "wake", "mail_send", "给安瞬的回信")
        al.append_act("cass", "chat", "diary_write", "x" * 999)
        al.append_act("default", "wake", "mail_read", "别人的")
        acts = al.recent_acts("cass")
        self.assertEqual([a["tool"] for a in acts], ["mail_send", "diary_write"])
        self.assertEqual(len(acts[1]["text"]), 200)   # 行为账短摘要
        self.assertEqual(al.recent_acts("cass", since_ts=int(time.time()) + 10), [])

    def test_cleanup(self):
        """超窗的整场（事件+截图）清掉；开着的段和窗内的段不动；行为账按行清。"""
        now = time.time()
        old_start = int(now) - 40 * 86400
        seg_old = al.open_segment("cass", "game", old_start)
        al.save_shot(seg_old, b"old")
        al.close_segment(seg_old)
        seg_new = al.open_segment("cass", "game", int(now) - 3600)
        al.append_event(seg_new, "comment", text="新")
        al.close_segment(seg_new)
        seg_open = al.open_segment("cass", "game", old_start - 100)
        al.append_event(seg_open, "comment", text="没收口的老段")
        # 行为账一老一新
        with open(al.ACT_DIR / "acts-cass.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": old_start, "tool": "mail_send"}) + "\n")
        al.append_act("cass", "wake", "mail_read", "新的")

        removed = al.cleanup(now=now)
        self.assertEqual(removed, 1)
        self.assertEqual(al.read_events(seg_old), [])
        self.assertFalse((al.SHOT_DIR / seg_old).exists())
        self.assertEqual(len(al.read_events(seg_new)), 1)
        self.assertEqual(len(al.read_events(seg_open)), 1)   # 开着=绝不清
        acts = al.recent_acts("cass")
        self.assertEqual([a["tool"] for a in acts], ["mail_read"])
        # 区间账（永存层）没被碰
        self.assertEqual(len(al.read_intervals("cass")), 2)


if __name__ == "__main__":
    unittest.main()
