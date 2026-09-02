"""引用逃逸：正文里被**引用**的标记不执行、也不剥（PLAN_native §14.0）。

2026-09-02 22:46 实锤：机主问小卡「你现在看到的首轮长什么样」，她用 ``` 围栏把注入
原样贴出来，里面 one_turn_hint 的例子 [[next_wake:1小时|…]] 被 parse_chat_next
当指令执行，顶掉了她自己 00:50 的钟；strip_markers 又把它剥掉，机主手机上只看到
一段带窟窿的话，当场没人发现。

这个文件锁两件事：
① 解析器分得出「使用」和「提及」；
② **prompt 里的例子全部解析不出来**——规避规则 3 的可执行版本。
"""
import unittest

import pipeline
import sse


REAL = "[[next_wake:1小时|给安瞬回信，回完更新名册]]"    # 09-02 那颗地雷的原文


class TestQuotedSpans(unittest.TestCase):
    def test_fence_and_inline(self):
        text = "前面 `a` 中间\n```\nx\n```\n后面"
        spans = pipeline.quoted_spans(text)
        self.assertEqual(len(spans), 2)

    def test_unclosed_fence_eats_to_end(self):
        text = "开头\n```\n" + REAL
        # 没闭合的围栏吃到结尾——贴到一半被截断的注入也算引用
        self.assertTrue(any(a <= text.index("[[") < b
                            for a, b in pipeline.quoted_spans(text)))

    def test_backtick_inside_fence_not_a_span(self):
        text = "```\n用 ` 单个反引号\n```"
        self.assertEqual(len(pipeline.quoted_spans(text)), 1)


class TestNextWake(unittest.TestCase):
    def test_fenced_not_executed_and_not_stripped(self):
        reply = f"原样贴给你：\n```\n{REAL}\n```\n就是这样"
        clean, mins, raw, todo = pipeline.parse_chat_next(reply)
        self.assertIsNone(mins)          # 不执行
        self.assertEqual(todo, "")
        self.assertIn(REAL, clean)       # 也不剥——剥了就是「带窟窿的正文」

    def test_inline_code_not_executed(self):
        clean, mins, _, _ = pipeline.parse_chat_next(f"用法是 `{REAL}` 这样")
        self.assertIsNone(mins)
        self.assertIn(REAL, clean)

    def test_unquoted_still_executes(self):
        clean, mins, _, todo = pipeline.parse_chat_next(f"好，{REAL}")
        self.assertEqual(mins, 60)
        self.assertEqual(todo, "给安瞬回信，回完更新名册")
        self.assertNotIn("[[", clean)

    def test_mixed_only_the_unquoted_one_counts(self):
        reply = f"```\n[[next_wake:8小时|引用的]]\n```\n[[next_wake:2小时|真要定的]]"
        _, mins, _, todo = pipeline.parse_chat_next(reply)
        self.assertEqual(mins, 120)
        self.assertEqual(todo, "真要定的")


class TestOtherMarkers(unittest.TestCase):
    def test_move_fenced(self):
        clean, target = pipeline.parse_chat_move("```\n[[move:kitchen]]\n```")
        self.assertIsNone(target)        # 复述一段文本不该真的搬家
        self.assertIn("[[move:kitchen]]", clean)

    def test_move_unquoted(self):
        _, target = pipeline.parse_chat_move("我去厨房 [[move:kitchen]]")
        self.assertEqual(target, "kitchen")

    def test_browser_fenced(self):
        clean, choice = pipeline.parse_browser_markers("`[[browser:close]]`")
        self.assertIsNone(choice)
        self.assertIn("[[browser:close]]", clean)

    def test_sticker_desc_fenced(self):
        h2i = {"s1": "abc"}
        clean, sends, updates = pipeline.parse_sticker_markers(
            "```\n[[sticker_desc:s1=改过的描述]]\n```", h2i)
        self.assertEqual(updates, [])
        self.assertEqual(sends, [])
        self.assertIn("[[sticker_desc:s1=改过的描述]]", clean)

    def test_strip_markers_keeps_quoted(self):
        self.assertIn(REAL, pipeline.strip_markers(f"```\n{REAL}\n```"))
        self.assertNotIn("[[", pipeline.strip_markers(f"话{REAL}"))


class TestStreamFilter(unittest.TestCase):
    def test_fenced_marker_reaches_the_bubble(self):
        f = sse.MarkerStreamFilter()
        out = f.feed("贴给你\n```\n") + f.feed(REAL) + f.feed("\n```\n完")
        self.assertIn(REAL, out)

    def test_unquoted_marker_hidden(self):
        f = sse.MarkerStreamFilter()
        self.assertNotIn("[[", f.feed(f"好的{REAL}就这样"))

    def test_fence_split_across_chunks(self):
        f = sse.MarkerStreamFilter()
        out = f.feed("``") + f.feed("`\n") + f.feed(REAL)
        self.assertIn(REAL, out)


class TestPromptExamplesAreInert(unittest.TestCase):
    """规避规则 3：**prompt 里所有例子都得解析不出来**。
    任何一个解析成功的，都是埋在文档里的地雷——09-02 那次就是这么炸的。"""

    def _assert_inert(self, text: str, where: str):
        _, mins, _, todo = pipeline.parse_chat_next(text)
        self.assertIsNone(mins, f"{where}: 例子能解析出时间，是地雷")
        self.assertEqual(todo, "", f"{where}: 例子能解析出待办，是地雷")
        _, target = pipeline.parse_chat_move(text)
        self.assertIsNone(target, f"{where}: 例子能解析出目的地，是地雷")
        _, choice = pipeline.parse_browser_markers(text)
        self.assertIsNone(choice, f"{where}: 例子能解析出浏览器去留，是地雷")

    def test_one_turn_hint_all_kinds(self):
        for kind in ("chat", "wake", "chat_session"):
            self._assert_inert(pipeline.one_turn_hint(kind), f"one_turn_hint({kind})")

    def test_chat_next_hint(self):
        self._assert_inert(pipeline._chat_next_hint(), "_chat_next_hint")

    def test_sticker_block_desc_example_is_inert(self):
        cat = [{"handle": "s1", "id": "abc", "description": "一只猫"}]
        block = pipeline.sticker_block(cat)
        _, _, updates = pipeline.parse_sticker_markers(block, {"s1": "abc"})
        self.assertEqual(updates, [],
                         "sticker_desc 的例子能改盘，是地雷（发表情那条是有意留真值的）")


if __name__ == "__main__":
    unittest.main()
