"""「你只有这一轮」+ NEXT 带待办（2026-08-28）单测。

背景：一次性 claude -p 里没有"说完之后"，模型却会写「我现在就去回信」——
实锤 08-28 21:49，同一条回复里开口前做的都成了、放到开口后的一件没做。
两刀：① 提示词把规矩写成二选一（先做后说 / 钉到下一轮）；
     ② 钉子带上「下一轮要做什么」，落 schedule.next_wake_todo，到点递回去。

这里只测机制（解析 / 落盘 / 回注 / 清除），不测模型听不听话。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_one_turn -v
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline
import state_store
import wake


class TestSplitNextRaw(unittest.TestCase):
    """时间 | 待办 的切分（三条路共用这一个口子）。"""

    def test_plain_time_has_no_todo(self):
        self.assertEqual(pipeline.split_next_raw("3小时"), ("3小时", ""))

    def test_halfwidth_and_fullwidth_pipe(self):
        for raw in ("3小时 | 给安瞬回信", "3小时｜给安瞬回信", "3小时|给安瞬回信"):
            self.assertEqual(pipeline.split_next_raw(raw), ("3小时", "给安瞬回信"), raw)

    def test_only_first_pipe_splits(self):
        head, todo = pipeline.split_next_raw("1小时 | 回信 | 再更新名册")
        self.assertEqual((head, todo), ("1小时", "回信 | 再更新名册"))

    def test_todo_is_capped(self):
        head, todo = pipeline.split_next_raw("1小时 | " + "活" * 500)
        self.assertEqual(len(todo), pipeline.NEXT_TODO_MAX)

    def test_empty(self):
        self.assertEqual(pipeline.split_next_raw(""), ("", ""))


class TestParseNextMinutes(unittest.TestCase):
    """待办里的数字不能被当成时间读走（兜底那条"有数字就当分钟"最容易咬）。"""

    def test_todo_digits_do_not_leak_into_time(self):
        # 「无 | 读第 2 封信」：不定点，别读成 2 分钟后醒
        self.assertIsNone(pipeline.parse_next_minutes("无 | 读第 2 封信"))

    def test_time_still_parsed_with_todo(self):
        self.assertEqual(pipeline.parse_next_minutes("90分钟 | 回信"), 90)
        self.assertEqual(pipeline.parse_next_minutes("3小时 | 回第 2 封"), 180)

    def test_clamped(self):
        self.assertEqual(pipeline.parse_next_minutes("1分钟 | x"), pipeline.NEXT_MIN_MIN)
        self.assertEqual(pipeline.parse_next_minutes("99小时 | x"), pipeline.NEXT_MAX_MIN)


class TestParseChatNext(unittest.TestCase):
    """聊天路的 [[next_wake:时间|待办]]。"""

    def test_marker_stripped_and_todo_returned(self):
        text, mins, raw, todo = pipeline.parse_chat_next(
            "读完了。[[next_wake:1小时|给安瞬回信，回完更新名册]]")
        self.assertEqual(text, "读完了。")
        self.assertEqual(mins, 60)
        self.assertEqual(raw, "1小时")            # 灰字只拿时间，待办不外漏
        self.assertEqual(todo, "给安瞬回信，回完更新名册")

    def test_no_todo(self):
        text, mins, raw, todo = pipeline.parse_chat_next("晚安 [[next_wake:8小时]]")
        self.assertEqual((text, mins, raw, todo), ("晚安", 480, "8小时", ""))

    def test_no_marker(self):
        self.assertEqual(pipeline.parse_chat_next("就一句话"), ("就一句话", None, None, ""))

    def test_last_valid_wins(self):
        _, mins, _, todo = pipeline.parse_chat_next(
            "[[next_wake:1小时|A]] 中间 [[next_wake:2小时|B]]")
        self.assertEqual((mins, todo), (120, "B"))

    def test_invalid_time_drops_todo_too(self):
        # 没有时点就没有钉子：待办跟着整条作废，不然它会永远注入、永远清不掉
        text, mins, raw, todo = pipeline.parse_chat_next("好 [[next_wake:无|回信]]")
        self.assertEqual((text, mins, raw, todo), ("好", None, None, ""))

    def test_note_carries_only_time(self):
        note = pipeline.next_wake_note("1小时", int(time.time()) + 3600)
        self.assertIn("1小时", note)
        self.assertNotIn("|", note)


class StateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oneturn_test_"))
        self._orig = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        self.cid = "default"

    def tearDown(self):
        state_store.CHAR_STATE_ROOT = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_todo(self, todo, at=None):
        state_store.write_schedule(
            {"next_wake_at": at or (int(time.time()) + 3600), "next_wake_todo": todo}, self.cid)


class TestPendingTodoBlock(StateBase):
    """钉着的钟 + 上一轮留的活，下一轮递回去。

    「钟点也要递」是 2026-09-01 补的：08-31 20:59 钉了 22:59 的钟，21:24 在聊天里
    又写了一句 [[next_wake:8小时]]，单槽位无条件覆盖 → 22:59 当场作废、看着像闹钟没响。
    钟没配待办时旧版整块不出现，等于让他闭着眼睛顶掉自己的承诺。"""

    # ⚠️ 09-04（§14.2）之后**两套措辞**：SDK 常驻路（kind != "wake"）＝一行状态行、
    # 纯知觉材料、一句规矩都不带（规矩挪进系统提示付一次）；-p 熄火回退路
    # （kind == "wake"）＝原文照旧（那条路没有系统提示可付）。

    def test_future_clock_is_announced_even_without_todo(self):
        state_store.write_schedule({"next_wake_at": int(time.time()) + 7200}, self.cid)
        block = pipeline.pending_todo_block(self.cid)
        self.assertIn("下一次醒来的闹钟", block)   # ← 没待办也得报，这条就是那次覆盖

    def test_nothing_pinned_is_empty(self):
        state_store.write_schedule({}, self.cid)
        self.assertEqual(pipeline.pending_todo_block(self.cid), "")

    def test_future_clock_carries_the_todo(self):
        self.set_todo("给安瞬回信")               # 默认钟点在一小时后
        block = pipeline.pending_todo_block(self.cid)
        self.assertIn("给安瞬回信", block)
        self.assertIn("下一次醒来的闹钟", block)

    def test_session_state_line_is_perception_only(self):
        """§14.2 的整个要点：状态行只报「手上有什么」，一句规矩都不带。
        规矩每轮重付一遍正是被砍掉的那份；「钟只有一个」归工具 schema 和回执
        （回执是在他动手那一刻报的，比任何静态句子准）。"""
        self.set_todo("给安瞬回信")
        block = pipeline.pending_todo_block(self.cid)
        self.assertEqual(block.count("\n"), 0, "状态行就该是一行")
        for rule in ("钟只有一个", "挪到新时间", "要么", "别默默留着", "action="):
            self.assertNotIn(rule, block, f"状态行里混进了规矩：{rule}")

    def test_wake_fallback_keeps_the_verbose_form(self):
        """-p 熄火回退路没有系统提示可付，规矩只能跟着注入走——顺手改它
        等于在改一条正在当降级兜底用的路。"""
        self.set_todo("给安瞬回信")
        block = pipeline.pending_todo_block(self.cid, kind="wake")
        self.assertIn("钉着一个钟", block)
        self.assertIn("NEXT", block)
        self.assertIn("钟只有一个", block)

    def test_due_clock_reads_as_this_is_that_next_turn(self):
        # 到点那轮（钟还没被 finish_wake_turn 消费）。措辞不许暗示他失约
        # （§14.3 口径：清槽是机制干的、不看他做没做）。
        self.set_todo("给安瞬回信", at=int(time.time()) - 10)
        block = pipeline.pending_todo_block(self.cid)
        self.assertIn("给安瞬回信", block)
        self.assertIn("闹钟到点了", block)
        for blame in ("失约", "忘了", "食言"):
            self.assertNotIn(blame, block)
        # 老路那支照旧
        self.assertIn("上一轮", pipeline.pending_todo_block(self.cid, kind="wake"))

    def test_due_clock_without_todo_is_empty(self):
        state_store.write_schedule({"next_wake_at": int(time.time()) - 10}, self.cid)
        self.assertEqual(pipeline.pending_todo_block(self.cid), "")

    def test_read_failure_is_swallowed(self):
        orig = state_store.read_schedule
        state_store.read_schedule = lambda *a, **kw: (_ for _ in ()).throw(OSError("盘挂了"))
        try:
            self.assertEqual(pipeline.pending_todo_block(self.cid), "")
        finally:
            state_store.read_schedule = orig


class TestInjectionIsPerceptionOnly(StateBase):
    """注入瘦身（PLAN_native §14.2，09-04）：每轮注入只剩**知觉材料**——
    几点、隔了多久、手上有什么。一句「关于他的说明」都没有。

    这条锁的是 chat_loop.py 那条纪律（「别往这儿加新的二手档案块」）的另一半：
    规矩和用法**付一次**（系统提示 / 工具 schema），不是每轮重付。"""

    def _inject(self):
        import chat_loop
        M = pipeline.Message
        now = int(time.time())
        return chat_loop.build_injection(
            [M(role="user", text="早", ts=now - 3480),
             M(role="user", text="在忙什么", ts=now)], self.cid)

    def test_no_rules_no_usage(self):
        self.set_todo("给安瞬回信")
        inj = self._inject()
        for banned in ("①", "②", "别把这件事说出口",      # 规矩 → 系统提示
                       "action=", "minutes", "[[next_wake",  # 用法 → 工具 schema
                       "钟只有一个", "挪到新时间"):
            self.assertNotIn(banned, inj, f"注入里混进了「{banned}」——那是付过一次的")

    def test_keeps_the_three_perceptions(self):
        self.set_todo("给安瞬回信")
        inj = self._inject()
        self.assertIn("距离上一条消息", inj)              # 隔了多久
        self.assertIn("下一次醒来的闹钟", inj)            # 手上有什么
        self.assertIn("给安瞬回信", inj)
        self.assertRegex(inj, r"【\d{2}-\d{2} 周. \d{2}:\d{2}】")   # 几点

    def test_time_head_has_no_daypart_word(self):
        """时间头用 stamp_str 不用 now_str：和 wake_injection 的抬头、和
        _stamp_times 重铸时补的锚行**天然同形**，他才认得出是同一种东西。"""
        inj = self._inject()
        self.assertNotIn("现在是", inj)
        for daypart in ("凌晨", "早上", "上午", "中午", "下午", "晚上", "深夜"):
            self.assertNotIn(daypart, inj)

    def test_no_leading_blank_line(self):
        self.assertFalse(self._inject().startswith("\n"))

    def test_stays_small(self):
        """379 → 143 字（plan 的实测数）。留个宽松上限当哨兵：这儿再长回去，
        多半是又有人把说明性文本塞进了每轮注入。"""
        self.set_todo("给安瞬回信，回完更新名册")
        self.assertLess(len(self._inject()), 200)


class TestOneTurnHint(unittest.TestCase):
    """两条路共用一份规矩，只有②的写法分叉。"""

    def test_chat_uses_the_tool(self):
        # 09-03（§14.1）：聊天那支②改成调工具。标记不再教——正文里的控制标记
        # 分不出「说」和「做」，工具调用天生分得出（§14.0 那次事故）。
        h = pipeline.one_turn_hint("chat")
        self.assertIn("next_wake", h)
        self.assertNotIn("[[next_wake", h)
        self.assertNotIn("NEXT:", h)

    def test_wake_uses_next_section(self):
        # -p 熄火回退路照旧：NEXT 段和 parse_wake_output 焊死，且那条 prompt 里
        # 没有第二种写法可并存
        h = pipeline.one_turn_hint("wake")
        self.assertIn("NEXT:", h)
        self.assertNotIn("[[next_wake:", h)

    def test_both_state_the_two_options(self):
        """机主 08-30：存在论开场白（你只有这一轮/进程结束）全线删掉，只留机制两条。"""
        for kind in ("chat", "wake"):
            h = pipeline.one_turn_hint(kind)
            self.assertNotIn("你只有这一轮", h)
            self.assertNotIn("进程", h)
            self.assertIn("①", h)
            self.assertIn("②", h)
            self.assertIn("别把这件事说出口", h)

    def test_session_variant_is_the_discipline_not_the_usage(self):
        """SDK 常驻路这一支 09-04（§14.2）从每轮注入挪进了**系统提示**，
        同时删掉用法半句——用法归工具 schema（那儿写得比这儿全），这里只剩纪律。
        「你只有这一轮/进程结束」的存在论解释照旧全删（机主 08-30 拍板）。"""
        h = pipeline.one_turn_hint("chat_session")
        self.assertIn("next_wake", h)
        self.assertNotIn("[[next_wake", h)   # 09-03（§14.1）：标记不再教
        self.assertNotIn("action=", h)       # 09-04（§14.2）：用法归 schema
        self.assertNotIn("minutes", h)
        self.assertNotIn("只有这一轮", h)
        self.assertNotIn("进程", h)
        self.assertIn("①", h)
        self.assertIn("②", h)
        self.assertIn("别把这件事说出口", h)


class TestWakeOutputTodo(StateBase):
    """醒来路：NEXT 带待办 → 落 schedule；点被消费 → 待办跟着清。"""

    def out(self, nxt):
        return f"THOUGHTS: 想了想\nACTION: none\nCONTENT: \nNEXT: {nxt}"

    def test_parse_keeps_full_raw(self):
        *_, next_min, next_raw = wake.parse_wake_output(self.out("3小时 | 回信"))
        self.assertEqual(next_min, 180)
        head, todo = pipeline.split_next_raw(next_raw)
        self.assertEqual((head, todo), ("3小时", "回信"))

    def wake_with(self, nxt, trigger="probability"):
        """真的走一趟 do_wake_sync（只把模型和外设打桩），断言落盘。"""
        settings = {"active_start": "08:00", "active_end": "23:00",
                    "day_freq": "mid", "night_freq": "low", "wake_window_n": 20,
                    "wake_daily_budget": None}
        orig = (wake.run_claude_wake, pipeline.tool_menu_block, pipeline.ombre_alive,
                wake.browser_keeper.apply_choice)
        wake.run_claude_wake = lambda prompt, char_id=None: (self.out(nxt), [])
        pipeline.tool_menu_block = lambda *a, **kw: ""
        pipeline.ombre_alive = lambda *a, **kw: False
        wake.browser_keeper.apply_choice = lambda *a, **kw: None
        try:
            return wake.do_wake_sync(settings, trigger, char_id=self.cid)
        finally:
            (wake.run_claude_wake, pipeline.tool_menu_block, pipeline.ombre_alive,
             wake.browser_keeper.apply_choice) = orig

    def test_new_pin_lands_time_and_todo(self):
        before = int(time.time())
        self.wake_with("3小时 | 给安瞬回信")
        sched = state_store.read_schedule(self.cid)
        self.assertAlmostEqual(sched["next_wake_at"], before + 180 * 60, delta=5)
        self.assertEqual(sched["next_wake_todo"], "给安瞬回信")

    def test_new_pin_without_todo_clears_old_one(self):
        # 钉子挪了地方，旧的活就作废——不然它会跟着新时点一路漂下去
        self.set_todo("旧的活")
        self.wake_with("1小时")
        self.assertEqual(state_store.read_schedule(self.cid)["next_wake_todo"], "")

    def test_consumed_scheduled_point_clears_todo(self):
        self.set_todo("回信", at=int(time.time()) - 5)   # 到点了，这次就是它叫醒的
        self.wake_with("无", trigger="scheduled")
        sched = state_store.read_schedule(self.cid)
        self.assertIsNone(sched["next_wake_at"])
        self.assertEqual(sched["next_wake_todo"], "")

    def test_early_wake_keeps_pending_todo(self):
        # 点还没到、被随机醒来提前叫起：活留着——他答应做事的那个时点还没来
        self.set_todo("回信", at=int(time.time()) + 3600)
        self.wake_with("无", trigger="probability")
        self.assertEqual(state_store.read_schedule(self.cid)["next_wake_todo"], "回信")

    def test_prompt_carries_todo_and_rule(self):
        self.set_todo("给安瞬回信")
        settings = {"active_start": "08:00", "active_end": "23:00",
                    "day_freq": "mid", "night_freq": "low", "wake_window_n": 20}
        orig_menu, orig_ombre = pipeline.tool_menu_block, pipeline.ombre_alive
        pipeline.tool_menu_block = lambda *a, **kw: ""
        pipeline.ombre_alive = lambda *a, **kw: False
        try:
            p = str(wake.wake_prompt(settings, char_id=self.cid))
        finally:
            pipeline.tool_menu_block, pipeline.ombre_alive = orig_menu, orig_ombre
        self.assertIn("别把这件事说出口", p)   # 一轮规矩还在（存在论开场白已删）
        self.assertIn("给安瞬回信", p)
        self.assertIn("3小时 | 给安瞬回信", p)   # NEXT 那行的写法示例


if __name__ == "__main__":
    unittest.main()
