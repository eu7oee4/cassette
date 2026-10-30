"""characters.notion_conf（一人一个 Notion 集成）单测：CHARS_DIR 指临时区，不碰真接线。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_notion_conf -v

这份的重点是**没有兜底**：Notion 的集成 token 决定「看得见哪些页面、编辑记录里是谁」，
env 兜底 = 两个角色静默共用同一个身份（角色串台那一类）。所以连默认角色也不吃 env。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import characters
import plugins


class NotionConfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="notion_conf_test_"))
        self._orig = characters.CHARS_DIR
        characters.CHARS_DIR = self.tmp
        for cid, meta in (("default", {}),
                          ("with_notion", {"notion": {"token": " ntn_abc123 "}}),
                          ("without", {"display_name": "x"})):
            d = self.tmp / cid
            d.mkdir(parents=True)
            (d / "char.json").write_text(json.dumps(meta), "utf-8")

    def tearDown(self):
        characters.CHARS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_configured_char_gets_own_token_stripped(self):
        self.assertEqual(characters.notion_conf("with_notion")["TOKEN"], "ntn_abc123")

    def test_unconfigured_char_gets_empty(self):
        self.assertEqual(characters.notion_conf("without")["TOKEN"], "")

    def test_no_env_fallback_even_for_default(self):
        # 兜底在这儿是有害的：默认角色也不许从环境变量捡一个别人的 token 来用
        for key in ("CASSETTE_NOTION_TOKEN", "NOTION_TOKEN", "NOTION_API_KEY"):
            os.environ[key] = "ntn_from_env"
        try:
            self.assertEqual(characters.notion_conf("default")["TOKEN"], "")
            self.assertEqual(characters.notion_conf("without")["TOKEN"], "")
        finally:
            for key in ("CASSETTE_NOTION_TOKEN", "NOTION_TOKEN", "NOTION_API_KEY"):
                os.environ.pop(key, None)

    def test_gate_blocks_char_without_token(self):
        # 没 token 的角色整个插件不挂（宁可工具不出现，也别让它带着别人的集成出门）
        self.assertTrue(plugins.PLUGIN_GATE["notion"]("with_notion"))
        self.assertFalse(plugins.PLUGIN_GATE["notion"]("without"))

    def test_env_hook_always_sends_the_key(self):
        # 值为空也要照发：不发的话子进程会静默继承后端进程环境里的同名值
        self.assertEqual(plugins.PLUGIN_ENV["notion"]("without"),
                         {"CASSETTE_NOTION_TOKEN": ""})


if __name__ == "__main__":
    unittest.main()
