"""skills（PLAN_skills S0）单测：frontmatter / 角色覆盖 / 场景过滤 / 路径逃逸 / 索引渲染。
一个真模型都不起、一个真状态都不碰（skills 库、角色注册表、state 根全指临时目录）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_skills -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import characters
import skills
import state_store


def _mk_skill(root: Path, name: str, front: dict, body: str = "正文",
              extra: dict | None = None) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---"] + [f"{k}: {v}" for k, v in front.items()] + ["---", "", body]
    (d / "SKILL.md").write_text("\n".join(lines), "utf-8")
    for rel, text in (extra or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, "utf-8")


class SkillsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skills_test_"))
        self._orig = (skills.SKILLS_DIR, characters.CHARS_DIR,
                      state_store.CHAR_STATE_ROOT)
        skills.SKILLS_DIR = self.tmp / "skills"
        characters.CHARS_DIR = self.tmp / "chars"
        state_store.CHAR_STATE_ROOT = self.tmp / "charstate"
        skills._warned.clear()

    def tearDown(self):
        (skills.SKILLS_DIR, characters.CHARS_DIR,
         state_store.CHAR_STATE_ROOT) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_char(self, cid: str) -> None:
        d = characters.CHARS_DIR / cid
        d.mkdir(parents=True, exist_ok=True)
        (d / "char.json").write_text("{}", "utf-8")


class TestList(SkillsBase):
    def test_basic_and_sorted(self):
        _mk_skill(skills.SKILLS_DIR, "bbb", {"name": "bbb", "description": "乙"})
        _mk_skill(skills.SKILLS_DIR, "aaa", {"name": "aaa", "description": "甲"})
        got = skills.list_skills()
        self.assertEqual([m["name"] for m in got], ["aaa", "bbb"])
        self.assertEqual(got[0]["description"], "甲")
        self.assertEqual(got[0]["when"], set(skills.CONTEXTS))   # 缺省全场景

    def test_when_filter(self):
        _mk_skill(skills.SKILLS_DIR, "cw", {"name": "cw", "description": "d",
                                            "when": "chat, wake"})
        self.assertEqual(len(skills.list_skills("chat")), 1)
        self.assertEqual(len(skills.list_skills("wake")), 1)
        self.assertEqual(skills.list_skills("code"), [])

    def test_bad_skills_skipped(self):
        _mk_skill(skills.SKILLS_DIR, "nodesc", {"name": "nodesc"})          # 缺 description
        _mk_skill(skills.SKILLS_DIR, "wrong", {"name": "other", "description": "d"})
        (skills.SKILLS_DIR / "notaskill").mkdir(parents=True)               # 没有 SKILL.md
        self.assertEqual(skills.list_skills(), [])

    def test_unknown_when_token_ignored(self):
        _mk_skill(skills.SKILLS_DIR, "s", {"name": "s", "description": "d",
                                           "when": "chat, brain"})
        got = skills.list_skills()
        self.assertEqual(got[0]["when"], {"chat"})


class TestOverlay(SkillsBase):
    def test_char_override_and_union(self):
        self._register_char("cass")
        _mk_skill(skills.SKILLS_DIR, "a", {"name": "a", "description": "共享版"})
        _mk_skill(skills.SKILLS_DIR, "b", {"name": "b", "description": "共享独有"})
        _mk_skill(characters.CHARS_DIR / "cass" / "skills", "a",
                  {"name": "a", "description": "角色版"})
        got = {m["name"]: m["description"] for m in skills.list_skills(char_id="cass")}
        self.assertEqual(got, {"a": "角色版", "b": "共享独有"})
        # 默认角色不受影响
        got_def = {m["name"]: m["description"] for m in skills.list_skills()}
        self.assertEqual(got_def["a"], "共享版")

    def test_unknown_char_falls_back(self):
        _mk_skill(skills.SKILLS_DIR, "a", {"name": "a", "description": "d"})
        self.assertEqual(len(skills.list_skills(char_id="不存在的角色")), 1)


class TestRead(SkillsBase):
    def setUp(self):
        super().setUp()
        _mk_skill(skills.SKILLS_DIR, "a", {"name": "a", "description": "d"},
                  body="路由表", extra={"prompts/x.md": "子流程"})

    def test_default_and_subfile(self):
        self.assertIn("路由表", skills.read("a"))
        self.assertEqual(skills.read("a", "prompts/x.md"), "子流程")

    def test_escape_rejected(self):
        (self.tmp / "secret.txt").write_text("秘密", "utf-8")
        for bad in ("../../secret.txt", "/etc/hosts", "prompts/../../a/SKILL.md/../../../secret.txt"):
            with self.assertRaises(ValueError):
                skills.read("a", bad)

    def test_unknown_skill_lists_names(self):
        with self.assertRaises(ValueError) as cm:
            skills.read("nope")
        self.assertIn("a", str(cm.exception))

    def test_missing_file_lists_files(self):
        with self.assertRaises(ValueError) as cm:
            skills.read("a", "prompts/nope.md")
        self.assertIn("prompts/x.md", str(cm.exception))


class TestIndex(SkillsBase):
    def test_renders_and_filters(self):
        _mk_skill(skills.SKILLS_DIR, "jobhunt",
                  {"name": "jobhunt", "description": "收到 JD → 先取流程", "when": "chat"})
        blk = skills.index_block("chat")
        self.assertIn("jobhunt：收到 JD → 先取流程", blk)
        self.assertIn(skills.SKILLS_MCP_TOOLS[0], blk)
        self.assertIn("skill_read", blk)
        self.assertEqual(skills.index_block("wake"), "")   # when 不含 wake → 整块不渲染

    def test_empty_library(self):
        self.assertEqual(skills.index_block("chat"), "")


class TestMcpConfig(SkillsBase):
    def test_writes_char_identity(self):
        self._register_char("cass")
        p = skills.mcp_config("cass")
        cfg = json.loads(p.read_text("utf-8"))
        srv = cfg["mcpServers"]["skills"]
        self.assertEqual(srv["env"]["CASSETTE_CHAR_ID"], "cass")
        self.assertTrue(srv["args"][0].endswith("skills_mcp.py"))
        self.assertTrue(str(p).startswith(str(self.tmp)))   # 落在测试临时区，没碰真 state


if __name__ == "__main__":
    unittest.main()
