"""闹钟工具化（PLAN_native §14.1）：`[[next_wake:]]` → `mcp__basics__next_wake`。

**第一位理由是 §14.0**：控制标记写在自然语言正文里，解析器分不出「说」和「做」——
09-02 22:46 她把自己的注入原样贴了一遍，里面的例子当场把钟改了。工具调用天生分得出。

这个文件锁四件：
① 工具本身四个动作（read / set / clear / 参数写歪）都有**回执**，且回执带绝对日期；
② 「钟只有一个」由回执说，且被顶掉的旧钟在回执里留名（规避规则 5）；
③ 小字口径：set/clear 出、read 不出，文案**带对象**不是裸名；
④ 三条 SDK prompt **一个字都不再教标记**，但解析器还认得（只读兼容）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_next_wake_tool -v
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import basics_mcp
import chat_loop
import pipeline
import state_store


def _call(**kw):
    """FastMCP 把函数包了一层，测的是底下那个真函数。"""
    fn = getattr(basics_mcp.next_wake, "fn", basics_mcp.next_wake)
    return fn(**kw)


class ToolBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nextwake_test_"))
        self._orig = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        self._orig_cid = basics_mcp.CHAR_ID
        basics_mcp.CHAR_ID = "default"

    def tearDown(self):
        state_store.CHAR_STATE_ROOT = self._orig
        basics_mcp.CHAR_ID = self._orig_cid
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sched(self):
        return state_store.read_schedule("default")


class TestClampShared(unittest.TestCase):
    """工具路和标记路共用一把夹子——小字是在 tool_use 那一刻自己按 minutes
    算时点的（那时还没有回执），两边夹法不一致，气泡报的钟点就和落盘的差一截。"""

    def test_range_and_rounding(self):
        self.assertEqual(pipeline.clamp_next_minutes(1), pipeline.NEXT_MIN_MIN)
        self.assertEqual(pipeline.clamp_next_minutes(99999), pipeline.NEXT_MAX_MIN)
        self.assertEqual(pipeline.clamp_next_minutes(60), 60)
        self.assertEqual(pipeline.clamp_next_minutes("90"), 90)

    def test_unusable_is_none(self):
        for bad in (0, -5, None, "", "一小时", object()):
            self.assertIsNone(pipeline.clamp_next_minutes(bad), bad)

    def test_marker_path_still_goes_through_it(self):
        self.assertEqual(pipeline.parse_next_minutes("1小时"), 60)
        self.assertEqual(pipeline.parse_next_minutes("999小时"), pipeline.NEXT_MAX_MIN)


class TestSlotRendering(unittest.TestCase):
    def test_always_carries_the_date(self):
        # 拍板一：回执写全绝对时间（含日期）——跨天重铸时「23:46」是歧义的
        at = int(time.time()) + 3600
        s = pipeline.alarm_slot_str(at, "给谁回信")
        self.assertIn(pipeline.fmt_ts(at), s)
        self.assertRegex(s, r"^\d{2}-\d{2} ")
        self.assertIn("「给谁回信」", s)

    def test_no_todo_no_quotes(self):
        at = int(time.time()) + 3600
        self.assertEqual(pipeline.alarm_slot_str(at, ""), pipeline.fmt_ts(at))


class TestTool(ToolBase):
    def test_read_when_empty(self):
        r = _call(action="read")
        self.assertTrue(r["ok"])
        self.assertFalse(r["set"])
        self.assertIn("没有钟", r["text"])

    def test_set_writes_and_receipts(self):
        r = _call(action="set", minutes=60, todo="给安瞬回信")
        self.assertEqual(self.sched()["next_wake_todo"], "给安瞬回信")
        at = self.sched()["next_wake_at"]
        self.assertAlmostEqual(at, time.time() + 3600, delta=5)
        # 回执当场报全绝对时间 + 待办：旧路是轮尾静默生效，他不知道自己定了什么
        self.assertIn(pipeline.fmt_ts(at), r["text"])
        self.assertIn("给安瞬回信", r["text"])
        self.assertIn("原来没有钟", r["text"])

    def test_set_names_the_clock_it_replaced(self):
        """§14.0 根因⑤ / 规避规则 5：单槽位覆盖，被顶掉的旧值不能凭空消失。"""
        _call(action="set", minutes=13, todo="读第 2 封信")
        old_at = self.sched()["next_wake_at"]
        r = _call(action="set", minutes=120, todo="新的活")
        self.assertIn(pipeline.fmt_ts(old_at), r["text"])   # 旧钟在回执里留名
        self.assertIn("读第 2 封信", r["text"])
        self.assertIn("钟只有一个", r["text"])               # 拍板二：由回执说，不进系统提示
        self.assertEqual(r["replaced"], pipeline.alarm_slot_str(old_at, "读第 2 封信"))

    def test_set_replaces_todo_wholesale(self):
        # 待办跟着时点整体替换：钉子换了地方，旧的那句活就作废
        _call(action="set", minutes=60, todo="旧的活")
        _call(action="set", minutes=90)
        self.assertEqual(self.sched()["next_wake_todo"], "")

    def test_clear_is_a_real_action_now(self):
        # 标记路根本没有「取消」的写法，只能跟机主说一句让人来清（§14.1 那张表第三行）
        _call(action="set", minutes=60, todo="给安瞬回信")
        r = _call(action="clear")
        self.assertIsNone(self.sched()["next_wake_at"])
        self.assertEqual(self.sched()["next_wake_todo"], "")
        self.assertIn("给安瞬回信", r["text"])   # 撤掉的是哪张，说清楚
        self.assertIn("没有钟", r["text"])

    def test_clear_when_empty_says_so(self):
        self.assertIn("本来就没有钟", _call(action="clear")["text"])

    def test_read_reports_the_gap(self):
        _call(action="set", minutes=60, todo="给安瞬回信")
        r = _call(action="read")
        self.assertTrue(r["set"])
        self.assertIn("给安瞬回信", r["text"])
        self.assertIn("后", r["text"])

    def test_read_does_not_write(self):
        _call(action="set", minutes=60, todo="给安瞬回信")
        before = dict(self.sched())
        _call(action="read")
        self.assertEqual(dict(self.sched()), before)

    def test_due_clock_does_not_blame_him(self):
        """§14.3 态③：清槽是机制干的、不看他做没做。到点未清只意味着那次醒来
        没走完（进程没跑 / _wake_dead 炸了 / 泵中避让）——措辞不许暗示他失约。"""
        state_store.write_schedule(
            {"next_wake_at": int(time.time()) - 600, "next_wake_todo": "读第 2 封信"},
            "default")
        r = _call(action="read")
        self.assertTrue(r["due"])
        self.assertIn("那次醒来还没走完", r["text"])
        for blame in ("失约", "没做", "忘了", "食言"):
            self.assertNotIn(blame, r["text"])

    def test_bad_minutes_raises_and_leaves_the_clock_alone(self):
        _call(action="set", minutes=60, todo="给安瞬回信")
        before = dict(self.sched())
        with self.assertRaises(ValueError) as cm:
            _call(action="set", minutes=0)
        self.assertIn("钟没有动", str(cm.exception))
        self.assertEqual(dict(self.sched()), before)

    def test_bad_action_raises(self):
        with self.assertRaises(ValueError):
            _call(action="snooze", minutes=60)

    def test_todo_is_capped(self):
        _call(action="set", minutes=60, todo="活" * 500)
        self.assertEqual(len(self.sched()["next_wake_todo"]), pipeline.NEXT_TODO_MAX)


class TestNoteText(unittest.TestCase):
    """小字（§14.1「要有小字 UI」）：定闹钟从此是一个看得见的动作。
    旧标记路在聊天里是隐形的——机主只在 next_wake_hint 那条返回值里瞟得到一眼，
    重看历史什么都没有。"""

    NAME = "mcp__basics__next_wake"

    def note(self, inp):
        return pipeline._stored_from_tool_use(self.NAME, inp)

    def test_set_carries_the_object_not_a_bare_name(self):
        s = self.note({"action": "set", "minutes": 60, "todo": "给安瞬回信"})
        self.assertEqual(s["tool"], "alarm")
        self.assertIn("定了闹钟", s["text"])
        self.assertIn("给安瞬回信", s["text"])
        self.assertRegex(s["text"], r"\d{2}-\d{2} \d{2}:\d{2}")   # 带日期的绝对时间

    def test_note_time_matches_what_the_tool_would_write(self):
        # 小字自己算时点（tool_use 那一刻还没有回执）：夹法必须和工具一致
        s = self.note({"action": "set", "minutes": 99999})
        at = int(time.time()) + pipeline.NEXT_MAX_MIN * 60
        self.assertIn(pipeline.fmt_ts(at), s["text"])

    def test_clear_shows(self):
        self.assertEqual(self.note({"action": "clear"}),
                         {"tool": "alarm", "text": "撤掉了闹钟"})

    def test_read_is_silent(self):
        self.assertIsNone(self.note({"action": "read"}))
        self.assertIsNone(self.note({}))          # 默认就是 read

    def test_broken_call_still_shows(self):
        # 规避规则 4：有副作用的动作不许静默失败。这次调用会炸，小字照出，
        # 失败原因由 tool_result 补在后面——不出小字＝他以为定了、机主也看不见。
        for inp in ({"action": "set", "minutes": 0},
                    {"action": "set"},
                    {"action": "snooze", "minutes": 60}):
            self.assertEqual(self.note(inp)["tool"], "alarm", inp)

    def test_not_a_memory_product(self):
        # 定钟不是记忆操作：进心流日志会污染醒来那份「别重复存」清单
        self.assertIn("alarm", pipeline.NON_MEMORY_TOOLS)

    def test_activity_log_summary_is_readable(self):
        # 行为账上这一行是单槽位覆盖的留痕，不能是 json.dumps 的兜底
        s = chat_loop._tool_summary(self.NAME,
                                    {"action": "set", "minutes": 60, "todo": "给安瞬回信"})
        self.assertTrue(s.startswith("set "))
        self.assertIn("给安瞬回信", s)
        self.assertNotIn("{", s)
        self.assertEqual(chat_loop._tool_summary(self.NAME, {"action": "read"}), "read")

    def test_alarm_is_worth_a_behaviour_line(self):
        self.assertTrue(pipeline.acts_worthy(self.NAME))


class TestMounting(unittest.TestCase):
    def test_mounted_everywhere_including_wake(self):
        """家选在 basics 是因为 BASICS_MCP_TOOLS 无条件打头 → 醒来轮的禁用面
        自动放行。「醒来恰恰是最该定下一次的时候」本来是最大的施工陷阱。"""
        self.assertIn("mcp__basics__next_wake", pipeline.BASICS_MCP_TOOLS)
        for ctx in ("chat", "wake"):
            self.assertIn("mcp__basics__next_wake",
                          pipeline.mounted_tool_names(ctx, "cass"), ctx)


class TestPromptsStoppedTeachingTheMarker(unittest.TestCase):
    """拍板三：**不能两条路并存**（pipeline.py 那个坑——同一规矩两处措辞，
    他照更近的写）。SDK 三条路改教工具，标记一个字不提。"""

    def _no_marker(self, text, where):
        self.assertNotIn("[[next_wake", text, f"{where}: 还在教标记")
        self.assertNotIn("[[下次醒来", text, f"{where}: 还在教标记")

    def test_system_prompt_hint(self):
        self._no_marker(pipeline._chat_next_hint(), "_chat_next_hint")
        self.assertIn("next_wake", pipeline._chat_next_hint())

    def test_one_turn_hint_chat_paths(self):
        for kind in ("chat", "chat_session"):
            t = pipeline.one_turn_hint(kind)
            self._no_marker(t, f"one_turn_hint({kind})")
            self.assertIn("next_wake", t)

    def test_wake_old_path_keeps_its_NEXT_section(self):
        # -p 熄火回退路的结构化输出格式，和 parse_wake_output 焊死；那条 prompt 里
        # 没有第二种写法可并存（_chat_next_hint 不进 wake_prompt）
        t = pipeline.one_turn_hint("wake")
        self._no_marker(t, "one_turn_hint(wake)")
        self.assertIn("NEXT", t)

    def test_parser_is_still_read_only_compatible(self):
        # 降级只读兼容：老路和历史文本里的标记还认得，只是不再教
        _, mins, _, todo = pipeline.parse_chat_next("好，[[next_wake:1小时|读第 2 封信]]")
        self.assertEqual(mins, 60)
        self.assertEqual(todo, "读第 2 封信")


class TestPendingBlockPointsAtTheTool(ToolBase):
    def test_future_clock_tells_him_how_to_move_it(self):
        _call(action="set", minutes=120, todo="给安瞬回信")
        b = pipeline.pending_todo_block("default")
        self.assertIn("next_wake", b)
        self.assertNotIn("[[next_wake", b)

    def test_due_todo_offers_clear(self):
        state_store.write_schedule(
            {"next_wake_at": int(time.time()) - 10, "next_wake_todo": "给安瞬回信"},
            "default")
        b = pipeline.pending_todo_block("default")
        self.assertIn("clear", b)                       # 撤得掉了，不用再求机主清
        self.assertIn("明说一句", pipeline.pending_todo_block("default", kind="wake"))


if __name__ == "__main__":
    unittest.main()
