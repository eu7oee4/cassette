"""/code/status 的 engine 字段（PLAN_gamechat §3.1，10-29 第 5 步）：
pump（unified 泵，骑在聊天 session 上）/ loop（独立 SDK loop）/ tmux（回退路）。
新包只在 pump 时把游戏中的消息改走 /chat/stream。code_bridge / plugins / characters
全桩掉——这条路由背后是生产状态文件（tests-reading-prod-state 雷）。"""
import types
import unittest


class CodeStatusEngineTest(unittest.TestCase):
    def setUp(self):
        import config
        from fastapi.testclient import TestClient
        import app as app_module
        self.app = app_module
        self._saved = {
            "key": config.AUTH_KEY, "code": config.CODE_MODE_ENABLED,
            "game": config.GAME_MODE_ENABLED,
            "cb": {k: getattr(app_module.code_bridge, k) for k in (
                "sdk_loop_handle", "session_alive", "tmux_available",
                "active_profile", "session_char", "is_busy")},
            "owner": app_module.plugins.owner_of,
            "name": app_module.characters.display_name,
        }
        config.AUTH_KEY = "test-key"
        config.GAME_MODE_ENABLED = True
        cb = app_module.code_bridge
        cb.session_alive = lambda: True
        cb.tmux_available = lambda: False
        cb.active_profile = lambda: "game"
        cb.session_char = lambda: "cass"
        cb.is_busy = lambda: False
        app_module.plugins.owner_of = lambda r: "cass"
        app_module.characters.display_name = lambda c: "Cassius"
        self.client = TestClient(app_module.app)
        self.h = {"X-Auth": "test-key"}

    def tearDown(self):
        import config
        s = self._saved
        config.AUTH_KEY, config.CODE_MODE_ENABLED, config.GAME_MODE_ENABLED = (
            s["key"], s["code"], s["game"])
        for k, v in s["cb"].items():
            setattr(self.app.code_bridge, k, v)
        self.app.plugins.owner_of = s["owner"]
        self.app.characters.display_name = s["name"]

    def _engine(self, handle):
        self.app.code_bridge.sdk_loop_handle = lambda: handle
        r = self.client.get("/code/status", headers=self.h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["engine"]

    def test_pump_loop_tmux(self):
        pump = types.SimpleNamespace(is_pump=True, awaiting_user_since=None, scene="game")
        loop = types.SimpleNamespace(is_pump=False, awaiting_user_since=None, scene="game")
        self.assertEqual(self._engine(pump), "pump")
        self.assertEqual(self._engine(loop), "loop")
        self.assertEqual(self._engine(None), "tmux")

    def test_profile_still_there(self):
        """旧包只认 profile——新字段不能挤掉老字段。"""
        pump = types.SimpleNamespace(is_pump=True, awaiting_user_since=None, scene="game")
        self.app.code_bridge.sdk_loop_handle = lambda: pump
        j = self.client.get("/code/status", headers=self.h).json()
        self.assertEqual(j["profile"], "game")
        self.assertEqual(j["session_char"], "cass")


if __name__ == "__main__":
    unittest.main()
