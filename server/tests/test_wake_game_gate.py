"""wake 的游戏会话避让闸：只拦**会话归属角色**，别人照常醒（2026-08-30 收窄）。

2026-09-02 再收一次：探针只认游戏会话了（session_mgr 的 "computer" 独占组），
不再探 tmux/code——code 不是模式、是一次写权限申请（08-31 改判），没有会话可避让。

护的是这次事故：08-30 小卡开着 code 会话写稿两个半钟头，Cassius 定在 15:46 的 NEXT
被压到 17:11 才兑现——他整个下午根本没在电脑前。老口径「会话开着拦所有人」是 M1 的
遗留（那会儿消息不分角色、会挤同屏），M2 之后不成立了。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_wake_code_gate -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import types

import code_bridge
import wake


class GameGateTest(unittest.TestCase):
    """game_session_owner / game_session_block 的三态：没开 / 归属明确 / 归属探不出。"""

    def setUp(self):
        self._handle = code_bridge.sdk_loop_handle
        self._busy = code_bridge.is_busy
        code_bridge.is_busy = lambda: False

    def tearDown(self):
        code_bridge.sdk_loop_handle = self._handle
        code_bridge.is_busy = self._busy

    def _session(self, alive: bool, char: str = ""):
        code_bridge.sdk_loop_handle = (
            (lambda: types.SimpleNamespace(char_id=char, scene="game"))
            if alive else (lambda: None))

    # ---------- owner 探针 ----------
    def test_no_session_is_none(self):
        self._session(alive=False, char="cass")   # 会话死了，上一场的归属不算数
        self.assertIsNone(wake.game_session_owner())

    def test_alive_returns_owner(self):
        self._session(alive=True, char="cass")
        self.assertEqual(wake.game_session_owner(), "cass")

    def test_alive_unknown_owner_is_empty_string(self):
        self._session(alive=True, char="")
        self.assertEqual(wake.game_session_owner(), "")   # 空串，不是 None

    def test_probe_failure_is_none(self):
        def boom():
            raise RuntimeError("探会话炸了")
        code_bridge.sdk_loop_handle = boom
        self.assertIsNone(wake.game_session_owner())      # 探不出来当没开，别静默困死

    # ---------- 避让判定（闸的判据，和 maybe_wake 里那行同形）----------
    @staticmethod
    def _avoids(cid: str) -> bool:
        owner = wake.game_session_owner()
        return owner is not None and owner in ("", cid)

    def test_owner_avoids_others_dont(self):
        self._session(alive=True, char="default")         # 小卡在电脑前
        self.assertTrue(self._avoids("default"))
        self.assertFalse(self._avoids("cass"))            # ← 这条就是那 85 分钟

    def test_unknown_owner_blocks_everyone(self):
        self._session(alive=True, char="")
        self.assertTrue(self._avoids("default"))
        self.assertTrue(self._avoids("cass"))             # 猜不出身份就退回老口径

    def test_no_session_blocks_nobody(self):
        self._session(alive=False)
        self.assertFalse(self._avoids("default"))
        self.assertFalse(self._avoids("cass"))

    # ---------- prompt 那段也得跟着收窄（别对没在电脑前的人说他在电脑前）----------
    def test_block_text_only_for_owner(self):
        self._session(alive=True, char="default")
        self.assertIn("会话", wake.game_session_block(True, "default"))
        self.assertEqual(wake.game_session_block(True, "cass"), "")

    def test_block_text_silent_when_owner_unknown(self):
        # 闸拦所有人、这段闭嘴：两边保守方向不同（别打扰 / 别说谎），不对称是故意的
        self._session(alive=True, char="")
        self.assertEqual(wake.game_session_block(True, "default"), "")
        self.assertEqual(wake.game_session_block(True, "cass"), "")


if __name__ == "__main__":
    unittest.main()
