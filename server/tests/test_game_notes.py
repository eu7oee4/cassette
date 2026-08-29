"""剧情笔记本 v2（PLAN_sdk §5.1）单测：三区读写、append-only、迁移、组合视图。
路径全指临时区（WorldBase 之训：模块的路径常量一个都不能漏 patch）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_game_notes -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import game_bridge as gb


class NotesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="game_notes_"))
        self._orig = (gb.STORY_DIR, gb.NOTES_PATH)
        gb.STORY_DIR = self.tmp / "story_notes"
        gb.NOTES_PATH = self.tmp / "notes.md"

    def tearDown(self):
        gb.STORY_DIR, gb.NOTES_PATH = self._orig


class NotesV2Test(NotesBase):
    def test_three_zones_and_caps(self):
        self.assertIsNone(gb.story_progress_write("第七章读到一半"))
        self.assertIsNone(gb.story_tips_write("跳过按钮在右上"))
        self.assertEqual(gb.story_progress_read(), "第七章读到一半")
        self.assertEqual(gb.story_tips_read(), "跳过按钮在右上")
        self.assertIn("字符", gb.story_progress_write("x" * (gb.STORY_PROGRESS_MAX + 1)))
        self.assertIn("字符", gb.story_tips_write("x" * (gb.STORY_TIPS_MAX + 1)))

    def test_chapter_append_only_numbering(self):
        n1, err = gb.story_chapter_append("第七章上", "脉络……")
        self.assertIsNone(err)
        n2, _ = gb.story_chapter_append("第七章下", "感想……")
        self.assertEqual((n1, n2), (1, 2))
        self.assertEqual(gb.story_chapters(), [(1, "第七章上"), (2, "第七章下")])
        self.assertIn("第七章下", gb.story_chapter_read(2))
        # 没有任何改旧篇的接口——这是断言 API 面，不是断言行为
        self.assertFalse(hasattr(gb, "story_chapter_write"))
        # 错误路径有声
        _, err = gb.story_chapter_append("", "没标题")
        self.assertIsNotNone(err)
        self.assertIn("没有第 9 篇", gb.story_chapter_read(9))

    def test_legacy_migration_once(self):
        gb.NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
        gb.NOTES_PATH.write_text("旧本：读到第六章，坐标 (100,200)", "utf-8")
        view = gb.story_view()                       # 第一次访问触发迁移
        self.assertIn("迁移自旧笔记本", view)
        self.assertIn("第六章", gb.story_chapter_read(0))
        # 旧 blob 原样保留（归任务本），迁移只发生一次
        self.assertIn("旧本", gb.NOTES_PATH.read_text("utf-8"))
        n, _ = gb.story_chapter_append("新的一场", "内容")
        self.assertEqual(n, 1)

    def test_view_progressive_disclosure(self):
        gb.story_progress_write("进度在这")
        for i in range(4):
            gb.story_chapter_append(f"第{i}场", f"内容{i}")
        view = gb.story_view(recent=2)
        self.assertIn("进度在这", view)
        self.assertIn("内容3", view)                 # 最近两篇全文
        self.assertIn("内容2", view)
        self.assertNotIn("内容0", view)              # 更早的只列标题
        self.assertIn("第0场", view)
        self.assertIn("game_chapter_read", view)

    def test_empty_view_guides(self):
        view = gb.story_view()
        self.assertIn("空白", view)
        self.assertIn("game_chapter_write", view)


if __name__ == "__main__":
    unittest.main()
