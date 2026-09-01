"""小字提醒的裸工具名口径（PLAN_chatui §7.2 拍板 2026-09-01）单测。

拍板三条：① 文案=全裸工具名（mcp_mail_send），零维护；② 失败读 ret（tool_result
定案的 ok/error）追加原因；③ 读类这期不下发（维持只发写类白名单，聚合下期绑包）。

这里锁：裸名换算、stored 条目带 name、sse memory 事件带 name、读类照旧一条不发。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_tool_notes -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline
import sse


def _assistant(name: str, inp: dict, tid: str = "t1") -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def _result(tid: str = "t1", error: str | None = None) -> dict:
    block = {"type": "tool_result", "tool_use_id": tid}
    if error is not None:
        block["is_error"] = True
        block["content"] = error
    return {"type": "user", "message": {"content": [block]}}


class TestBareToolName(unittest.TestCase):
    def test_mcp_collapses_server_segment(self):
        # server 段是挂载编号不是工具身份：mcp__mail__mail_send → mcp_mail_send
        self.assertEqual(pipeline.bare_tool_name("mcp__mail__mail_send"), "mcp_mail_send")
        self.assertEqual(pipeline.bare_tool_name("mcp__ombre-brain__hold"), "mcp_hold")

    def test_builtin_stays_as_is(self):
        self.assertEqual(pipeline.bare_tool_name("Bash"), "Bash")
        self.assertEqual(pipeline.bare_tool_name("Edit"), "Edit")


class TestStoredCarriesName(unittest.TestCase):
    def test_settled_item_has_bare_name(self):
        c = pipeline.StoredCollector()
        c.on_assistant(_assistant("mcp__mail__mail_send", {"to": "a@b.c", "subject": "hi"}))
        settled = c.on_user(_result())
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["name"], "mcp_mail_send")
        self.assertEqual(settled[0]["tool"], "mail")   # 标签不动：改判/日志还认它

    def test_failed_item_keeps_name_and_error(self):
        # ② 失败=ret 定案：ok=False + error 原话，小字追加原因就吃这两个字段
        c = pipeline.StoredCollector()
        c.on_assistant(_assistant("mcp__mail__mail_send", {"to": "a@b.c"}))
        settled = c.on_user(_result(error="配额满了"))
        self.assertFalse(settled[0]["ok"])
        self.assertEqual(settled[0]["name"], "mcp_mail_send")
        self.assertIn("配额", settled[0]["error"])

    def test_read_tools_still_silent(self):
        # ③ 读类这期不下发：breath/mail_read 不进 stored，一条小字都没有
        c = pipeline.StoredCollector()
        c.on_assistant(_assistant("mcp__ombre-brain__breath", {}))
        c.on_assistant(_assistant("mcp__mail__mail_read", {"id": "1"}, tid="t2"))
        self.assertEqual(c.on_user(_result()), [])
        self.assertEqual(c.on_user(_result(tid="t2")), [])


class TestMemoryEventName(unittest.TestCase):
    def test_sse_memory_event_carries_name(self):
        chunks = list(sse._memory_events(
            {"tool": "mail", "text": "给 a@b.c", "name": "mcp_mail_send", "ok": True}))
        self.assertEqual(len(chunks), 1)
        ev = json.loads(chunks[0].decode("utf-8").removeprefix("data: "))
        self.assertEqual(ev["type"], "memory")
        self.assertEqual(ev["name"], "mcp_mail_send")


if __name__ == "__main__":
    unittest.main()
