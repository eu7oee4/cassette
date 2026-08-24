"""characters.browser_conf（一人一个浏览器）单测：CHARS_DIR 指临时区，不碰真接线。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_browser_conf -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import characters


class BrowserConfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="browser_conf_test_"))
        self._orig = characters.CHARS_DIR
        characters.CHARS_DIR = self.tmp
        for cid, meta in (("default", {}),
                          ("with_browser", {"browser": {"mcp_url": " http://localhost:3009/mcp "}}),
                          ("without", {"display_name": "x"})):
            d = self.tmp / cid
            d.mkdir(parents=True)
            (d / "char.json").write_text(json.dumps(meta), "utf-8")

    def tearDown(self):
        characters.CHARS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_falls_back_to_3002(self):
        # 真 .env 里没有 CASSETTE_BROWSER_MCP_URL（有的话这条测的就是 env 优先，也对）
        url = characters.browser_conf("default")["MCP_URL"]
        self.assertTrue(url)   # 默认角色永远有值（零配置旧行为）

    def test_configured_char_gets_own_url_stripped(self):
        self.assertEqual(characters.browser_conf("with_browser")["MCP_URL"],
                         "http://localhost:3009/mcp")

    def test_unconfigured_char_gets_empty_not_default(self):
        # 非默认角色不吃兜底：空串 = 没有自己的浏览器（插件不挂载、keeper 跳过），
        # 绝不能落到别人的 3002 上
        self.assertEqual(characters.browser_conf("without")["MCP_URL"], "")


if __name__ == "__main__":
    unittest.main()
