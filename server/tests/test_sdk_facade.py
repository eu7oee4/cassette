"""code_bridge 的 SDK loop 门面（PLAN_sdk S1/PR7）单测：loop 活着时四个判据函数
（session_alive/active_profile/session_char/session_started_at）都答 loop 的，
没有时回落 tmux 老路。wake 避让/cohabit 在场/app 对齐全走这一个出口。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_sdk_facade -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import code_bridge
import session_mgr as sm


class FacadeTest(unittest.TestCase):
    def setUp(self):
        sm._registry.clear()
        self._read_orig = code_bridge._read_session_state
        code_bridge._read_session_state = lambda: {}   # tmux 侧状态钉空，别读真文件

    def tearDown(self):
        sm._registry.clear()
        code_bridge._read_session_state = self._read_orig

    def test_loop_alive_answers_all(self):
        h = sm.LoopHandle(char_id="cass", scene="game", exclusive_group="computer")
        sm._registry[("cass", "game")] = h              # task=None＝起飞中，算活着
        self.assertTrue(code_bridge.session_alive())
        self.assertEqual(code_bridge.active_profile(), "game")
        self.assertEqual(code_bridge.session_char(), "cass")
        self.assertEqual(code_bridge.session_started_at(), int(h.started_at))

    def test_no_loop_falls_back(self):
        self.assertIsNone(code_bridge.sdk_loop_handle())
        self.assertEqual(code_bridge.active_profile(), "code")   # 状态空 → 默认档案
        self.assertEqual(code_bridge.session_char(), "")
        self.assertEqual(code_bridge.session_started_at(), 0)

    def test_non_computer_group_invisible(self):
        # chat loop 在注册表里但没拿游戏——不该被当成电脑会话
        sm._registry[("cass", "chat")] = sm.LoopHandle(char_id="cass", scene="chat")
        self.assertIsNone(code_bridge.sdk_loop_handle())

    def test_game_pump_projected(self):
        """PR13：骑在 chat 上的泵在门面上=「电脑上的会话」，scene 报 game；
        控制类操作走显式语义（置旗/拒终端口），绝不 cancel 聊天 loop。"""
        ch = sm.LoopHandle(char_id="cass", scene="chat")
        ch.meta["game_pump"] = {"seg_id": "s", "start_ts": 12345.0}
        ch.meta["shots"] = 3
        sm._registry[("cass", "chat")] = ch
        h = code_bridge.sdk_loop_handle()
        self.assertIsNotNone(h)
        self.assertTrue(h.is_pump)
        self.assertTrue(code_bridge.session_alive())
        self.assertEqual(code_bridge.active_profile(), "game")
        self.assertEqual(code_bridge.session_char(), "cass")
        self.assertEqual(code_bridge.session_started_at(), 12345)
        self.assertEqual(h.meta.get("shots"), 3)
        h.request_stop("manual")
        self.assertTrue(ch.meta.get("end_requested"))
        with self.assertRaises(RuntimeError):
            h.put_threadsafe("hi")
        # 独立 loop 优先于泵（共存期两者互斥由起手路径保证，门面按老语义先报独立的）
        g = sm.LoopHandle(char_id="cass", scene="game", exclusive_group="computer")
        sm._registry[("cass", "game")] = g
        self.assertIs(code_bridge.sdk_loop_handle(), g)


if __name__ == "__main__":
    unittest.main()
