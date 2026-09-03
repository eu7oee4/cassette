"""闹钟补铸（PLAN_native §14.4）：重铸之后，他做过的那次「定钟」还在他的记忆里。

**为什么需要它**：重铸的输入是 recent_window——app 的扁平消息窗口（role/text/ts），
-p 时期的形状，里面天然没有工具调用。所以「我 22:46 定了 00:50 的钟」这件事一重铸
就蒸发，只剩注入的状态行兜着。图是第一例（`save_chat_images` 留底 + `_merge_images`
回填），闹钟是第二例，套路一模一样：**执行层留底，渲染层回填**。

前置闸 2026-09-03 一手实证解除（`tools/forge_swap_probe.py`）：成对的 tool_use/
tool_result 铸得进去、CLI 和 agent-sdk 两条路都真到了模型眼前。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_alarm_reforge -v
"""
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat_loop
import forge
import state_store

NAME = "mcp__basics__next_wake"


def _events(path: Path) -> list:
    return [json.loads(l) for l in path.read_text("utf-8").splitlines() if l.strip()]


def _blocks(ev) -> list:
    return ev["message"]["content"]


class TestForgeCastsToolPairs(unittest.TestCase):
    """`forge.render` 把 tools 铸成真 transcript 里那一轮**本来的形状**。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alarmforge_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render(self, msgs):
        sid = forge.render(msgs, cwd="/probe/cwd", projects_root=self.tmp)
        return _events(forge.transcript_path("/probe/cwd", sid, self.tmp))

    def test_three_event_shape_and_order(self):
        evs = self._render([
            {"role": "user", "text": "那你自己定个时间", "ts": 1000},
            {"role": "assistant", "text": "定好了。",
             "tools": [{"name": NAME,
                        "input": {"action": "set", "minutes": 120, "todo": "回信"},
                        "result": "定在 09-03 00:50「回信」。原来没有钟。"}]}])
        self.assertEqual([e["type"] for e in evs],
                         ["user", "assistant", "user", "assistant"])
        # 调用**排在收场白前面**——反过来读起来像「说完了才去做」
        self.assertEqual(_blocks(evs[1])[0]["type"], "tool_use")
        self.assertEqual(_blocks(evs[1])[0]["name"], NAME)
        self.assertEqual(_blocks(evs[1])[0]["input"]["todo"], "回信")
        self.assertEqual(_blocks(evs[2])[0]["type"], "tool_result")
        self.assertEqual(_blocks(evs[3])[0]["text"], "定好了。")
        # id 对得上（孤儿 = 首次请求 400）
        self.assertEqual(_blocks(evs[2])[0]["tool_use_id"],
                         _blocks(evs[1])[0]["id"])

    def test_receipt_rides_along(self):
        evs = self._render([
            {"role": "user", "text": "定个钟", "ts": 1000},
            {"role": "assistant", "text": "好。",
             "tools": [{"name": NAME, "input": {"action": "clear"},
                        "result": "撤掉了 09-03 00:50「回信」，现在没有钟。"}]}])
        self.assertIn("撤掉了", _blocks(evs[2])[0]["content"])

    def test_tools_only_on_assistant(self):
        with self.assertRaises(ValueError):
            self._render([{"role": "user", "text": "x", "ts": 1,
                           "tools": [{"name": NAME, "input": {}, "result": ""}]}])

    def test_tool_free_history_is_byte_identical(self):
        """确定性红线：不带工具的历史，铸出来必须和这次改动之前一模一样。
        session_id 是内容 digest——工具那一维只在真有工具时才参与，
        否则全仓每一份历史的 id 都会因为这次改动跳一遍。"""
        msgs = [{"role": "user", "text": "在吗", "ts": 1000},
                {"role": "assistant", "text": "在"}]
        sid = forge.render(msgs, cwd="/x/y", projects_root=self.tmp)
        self.assertEqual(sid, "6b583edf-b660-5e78-a349-9000046fe1af")

    def test_tools_change_the_session_id(self):
        base = [{"role": "user", "text": "在吗", "ts": 1000},
                {"role": "assistant", "text": "在"}]
        withtool = [base[0], {**base[1],
                              "tools": [{"name": NAME, "input": {"action": "clear"},
                                         "result": "本来就没有钟。"}]}]
        self.assertNotEqual(
            forge.render(base, cwd="/x/y", projects_root=self.tmp),
            forge.render(withtool, cwd="/x/y", projects_root=self.tmp))


class StateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alarmstate_"))
        self._orig = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        self.cid = "default"

    def tearDown(self):
        state_store.CHAR_STATE_ROOT = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stash(self, ts, tid="t1", action="set", minutes=120, todo="回信",
              ret="定在 09-03 00:50「回信」。原来没有钟。"):
        state_store.append_alarm_call(
            {"ts": ts, "id": tid, "name": NAME, "ok": True, "ret": ret,
             "input": {"action": action, "minutes": minutes, "todo": todo}},
            self.cid)


class TestStash(StateBase):
    def test_roundtrip_and_dedupe(self):
        self.stash(1000, "t1")
        self.stash(1000, "t1")     # 流里同一条 assistant 会带累计块重复发
        self.stash(2000, "t2")
        rows = state_store.read_alarm_calls(self.cid)
        self.assertEqual([r["id"] for r in rows], ["t1", "t2"])

    def test_missing_file_is_empty_not_a_crash(self):
        self.assertEqual(state_store.read_alarm_calls(self.cid), [])


class TestMergeBack(StateBase):
    def hist(self, base=10_000):
        return [{"role": "user", "text": "那你自己定个时间", "ts": base},
                {"role": "assistant", "text": "定好了。", "ts": base + 3}]

    def test_attaches_to_that_turn(self):
        self.stash(10_001)                     # 调用发生在轮中
        out = chat_loop._merge_alarm_calls(self.hist(), self.cid)
        self.assertNotIn("tools", out[0])      # user 那条不挂
        self.assertEqual(out[1]["tools"][0]["name"], NAME)
        self.assertEqual(out[1]["tools"][0]["input"]["todo"], "回信")

    def test_wake_turn_stamps_the_assistant_before_the_call(self):
        """醒来轮的 assistant 用 started_ts——**早于**调用。所以认领只能取
        「最近的一条」，取「之后的第一条」会整类漏掉。"""
        h = [{"role": "user", "text": "x", "ts": 9_000},
             {"role": "assistant", "text": "独白", "ts": 10_000}]
        self.stash(10_050)                     # 调用在 assistant 的 ts 之后
        out = chat_loop._merge_alarm_calls(h, self.cid)
        self.assertIn("tools", out[1])

    def test_rolled_out_of_the_window_is_not_cast(self):
        """那一轮已经滚出窗口 → 不铸，回落到状态行。别为了留住它统一铸到末尾
        装成「刚做的」——那从保留记忆变成编造时序了。"""
        self.stash(10_001)
        far = [{"role": "user", "text": "后来的话", "ts": 90_000},
               {"role": "assistant", "text": "后来的回话", "ts": 90_003}]
        self.assertEqual(chat_loop._merge_alarm_calls(far, self.cid), far)

    def test_cast_as_it_happened_not_as_the_slot_looks_now(self):
        """**照实铸，不看当前槽位**：「我定了 00:50 的钟」这件事是真的，
        后来钟被谁清了不影响它是真的。假的是拿旧回执冒充当前状态，
        那是注入状态行的职责（§14.3），不是记忆的。"""
        self.stash(10_001)
        state_store.write_schedule({"next_wake_at": None, "next_wake_todo": ""},
                                   self.cid)
        out = chat_loop._merge_alarm_calls(self.hist(), self.cid)
        self.assertIn("tools", out[1])

    def test_two_calls_one_turn(self):
        self.stash(10_001, "t1", todo="旧的活")
        self.stash(10_002, "t2", todo="新的活")
        out = chat_loop._merge_alarm_calls(self.hist(), self.cid)
        self.assertEqual([t["input"]["todo"] for t in out[1]["tools"]],
                         ["旧的活", "新的活"])

    def test_nothing_stashed_is_a_no_op(self):
        h = self.hist()
        self.assertEqual(chat_loop._merge_alarm_calls(h, self.cid), h)


class TestWholeRenderChain(StateBase):
    """整条铸造链跑一遍：留底 → 四道渲染 → JSONL 里真有成对的工具块。
    单测 `_merge_alarm_calls` 不够——下游三道（并图/打戳/活动框）任何一道把
    未知键丢掉，工具块就静默蒸发，而那种失败在生产里只表现为「他不记得了」。"""

    def test_tool_blocks_reach_the_jsonl(self):
        self.stash(10_001)
        history = [{"role": "user", "text": "那你自己定个时间", "ts": 10_000},
                   {"role": "assistant", "text": "定好了。", "ts": 10_003}]
        rendered = chat_loop._frame_activities(
            chat_loop._stamp_times(chat_loop._merge_images(
                chat_loop._merge_alarm_calls(history, self.cid), self.cid),
                self.cid),
            self.cid)
        out = Path(tempfile.mkdtemp(prefix="alarmchain_"))
        try:
            sid = forge.render(rendered, cwd="/probe/cwd", projects_root=out)
            evs = _events(forge.transcript_path("/probe/cwd", sid, out))
            kinds = [b["type"] for e in evs for b in _blocks(e)]
            self.assertIn("tool_use", kinds)
            self.assertIn("tool_result", kinds)
            uses = [b for e in evs for b in _blocks(e) if b["type"] == "tool_use"]
            self.assertEqual(uses[0]["input"]["todo"], "回信")
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
