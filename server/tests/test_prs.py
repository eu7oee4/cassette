"""prs（PLAN_prs §2/§3）：工作副本 + PR 记录 + 工具真身。全部在临时 git 仓里跑，不碰真仓。

主线：串台（别人的 PR 关不了、副本/分支带角色名）、description 必填、skip-worktree 文件拒、
每角色 3 个上限、同一分支再 open = 更新同一个 PR、切分支纪律（脏副本拒 / discard / 草稿分支清）、
测试比基线（干净检出本来就挂的不算）、main 动过的重放（顺 / 打架留标记）、列表排序。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_prs -v
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prs

_SUITE = '''import unittest
import mod

class T(unittest.TestCase):
    def test_value(self):
        self.assertEqual(mod.VALUE, 1)

    def test_env_born_broken(self):
        # 干净检出里天生挂的那类（真仓里是缺 .env / 角色配置）
        self.assertTrue(False)
'''


# 沙箱探针：测试代码想偷主目录的凭据、改主目录、往外发——全都该失败
_PROBE = '''import os, tempfile, unittest, urllib.request
MAIN = {main!r}

class P(unittest.TestCase):
    def test_cannot_read_main_secret(self):
        with self.assertRaises(OSError):
            open(os.path.join(MAIN, "server", ".env")).read()

    def test_cannot_write_main(self):
        with self.assertRaises(OSError):
            open(os.path.join(MAIN, "server", "_pwn.py"), "w")

    def test_no_network(self):
        with self.assertRaises(OSError):
            urllib.request.urlopen("https://example.com", timeout=5)

    def test_can_write_own_worktree_and_tmp(self):
        open("_ok.txt", "w").write("x")
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"x")
'''


def _g(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout


class PRBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        (self.repo / "server" / "tests").mkdir(parents=True)
        (self.repo / "ios").mkdir()
        (self.repo / "server" / "mod.py").write_text("VALUE = 1\n")
        (self.repo / "server" / "notes.txt").write_text("a\nb\nc\n")
        (self.repo / "server" / "tests" / "__init__.py").write_text("")
        (self.repo / "server" / "tests" / "test_mod.py").write_text(_SUITE)
        (self.repo / "ios" / "Info.plist").write_text("<plist/>\n")
        (self.repo / ".gitignore").write_text("__pycache__/\n.worktrees/\nstate/\n")
        _g(self.repo, "init", "-q", "-b", "main")
        _g(self.repo, "add", "-A")
        _g(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        _g(self.repo, "update-index", "--skip-worktree", "ios/Info.plist")
        self._orig = (prs.REPO_ROOT, prs.PRS_DIR)
        prs.REPO_ROOT = self.repo
        prs.PRS_DIR = self.tmp / "prs"

    def tearDown(self):
        prs.REPO_ROOT, prs.PRS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wt(self, char):
        return self.repo / ".worktrees" / char

    def edit(self, char, rel, text):
        p = self.wt(char) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def open_one(self, char, slug, rel="server/notes.txt", text="changed\n"):
        prs.start(char, slug)
        self.edit(char, rel, text)
        return prs.open_pr(char, f"改 {slug}", "让机主看到的变化")

    def commit_on_main(self, rel, text):
        (self.repo / rel).write_text(text)
        _g(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "main moves")


class TestStart(PRBase):
    def test_worktree_per_char_and_branch_from_main(self):
        r = prs.start("default", "fix-a")
        self.assertEqual(r["path"], str(self.wt("default")))
        self.assertEqual(r["branch"], "wake/default/fix-a")
        self.assertEqual(_g(self.wt("default"), "rev-parse", "HEAD"),
                         _g(self.repo, "rev-parse", "main"))
        prs.start("cass", "fix-a")
        self.assertEqual(prs._current_branch(self.wt("cass")), "wake/cass/fix-a")
        self.assertEqual(prs._current_branch(self.wt("default")), "wake/default/fix-a")

    def test_bad_ids_refused(self):
        for char in ("../x", "", "A", "_baseline"):
            with self.assertRaises(prs.PRError):
                prs.start(char, "ok")
        for slug in ("Bad", "a b", "../x", "-x", ""):
            with self.assertRaises(prs.PRError):
                prs.start("default", slug)

    def test_dirty_worktree_blocks_switch_until_discard(self):
        prs.start("default", "one")
        self.edit("default", "server/notes.txt", "draft\n")
        with self.assertRaises(prs.PRError) as cm:
            prs.start("default", "two")
        self.assertIn("discard", str(cm.exception))
        prs.start("default", "two", discard=True)
        self.assertEqual((self.wt("default") / "server/notes.txt").read_text(), "a\nb\nc\n")
        # 从没开过 PR 的草稿分支切走即删
        self.assertFalse(prs._branch_exists("wake/default/one"))

    def test_restart_same_draft_keeps_edits(self):
        prs.start("default", "one")
        self.edit("default", "server/notes.txt", "draft\n")
        prs.start("default", "one")
        self.assertEqual((self.wt("default") / "server/notes.txt").read_text(), "draft\n")

    def test_limit_three_unfinished(self):
        for s in ("a", "b", "c"):
            self.open_one("default", s)
        with self.assertRaises(prs.PRError) as cm:
            prs.start("default", "d")
        self.assertIn("3", str(cm.exception))
        prs.start("cass", "d")  # 别的角色不受影响
        prs.close("default", 1, "不要了")
        prs.start("default", "d")

    def test_used_name_refused_after_close(self):
        self.open_one("default", "a")
        prs.close("default", 1, "不要了")
        with self.assertRaises(prs.PRError):
            prs.start("default", "a")


class TestOpen(PRBase):
    def test_open_commits_only_changes_and_records(self):
        prs.start("default", "a")
        self.edit("default", "server/notes.txt", "a\nB\nc\n")
        self.edit("default", "server/new.py", "X = 1\n")
        r = prs.open_pr("default", "标题", "描述")
        self.assertEqual(r["id"], 1)
        self.assertTrue(r["created"])
        self.assertEqual(sorted(r["files"]), ["server/new.py", "server/notes.txt"])
        rec = json.loads((self.tmp / "prs" / "1.json").read_text())
        self.assertEqual(rec["char"], "default")
        self.assertEqual(rec["branch"], "wake/default/a")
        self.assertEqual(rec["status"], "open")
        self.assertFalse(rec["touches_ios"])
        self.assertTrue(rec["tests"]["ok"], rec["tests"]["summary"])
        # 跑测试生成的 __pycache__/state 不进提交；主目录不动
        self.assertEqual(prs._dirty_paths(self.wt("default")), [])
        self.assertEqual((self.repo / "server/notes.txt").read_text(), "a\nb\nc\n")
        author = _g(self.repo, "log", "-1", "--format=%an", "wake/default/a").strip()
        self.assertEqual(author, "default")

    def test_description_and_title_required(self):
        prs.start("default", "a")
        self.edit("default", "server/notes.txt", "x\n")
        with self.assertRaises(prs.PRError):
            prs.open_pr("default", "标题", "   ")
        with self.assertRaises(prs.PRError):
            prs.open_pr("default", "", "描述")

    def test_nothing_to_open(self):
        prs.start("default", "a")
        with self.assertRaises(prs.PRError):
            prs.open_pr("default", "t", "d")

    def test_skip_worktree_file_refused(self):
        prs.start("default", "a")
        self.edit("default", "ios/Info.plist", "<plist>changed</plist>\n")
        with self.assertRaises(prs.PRError) as cm:
            prs.open_pr("default", "t", "d")
        self.assertIn("ios/Info.plist", str(cm.exception))
        self.assertEqual(prs._all_records(), [])

    def test_touches_ios_flag(self):
        r = self.open_one("default", "a", rel="ios/View.swift", text="//\n")
        self.assertTrue(prs.get(r["id"])["touches_ios"])

    def test_reopen_same_branch_updates_same_pr(self):
        self.open_one("default", "a")
        self.edit("default", "server/notes.txt", "again\n")
        r = prs.open_pr("default", "新标题", "新描述")
        self.assertEqual(r["id"], 1)
        self.assertFalse(r["created"])
        self.assertEqual(prs.get(1)["title"], "新标题")
        self.assertEqual(len(prs._all_records()), 1)

    def test_ids_global_across_chars(self):
        self.assertEqual(self.open_one("default", "a")["id"], 1)
        self.assertEqual(self.open_one("cass", "a")["id"], 2)

    def test_not_on_pr_branch(self):
        prs._ensure_worktree("default")
        self.edit("default", "server/notes.txt", "x\n")
        with self.assertRaises(prs.PRError):
            prs.open_pr("default", "t", "d")


class TestCloseListView(PRBase):
    def test_only_own_pr_closable(self):
        self.open_one("default", "a")
        with self.assertRaises(prs.PRError) as cm:
            prs.close("cass", 1, "关掉")
        self.assertIn("不是你提的", str(cm.exception))
        self.assertEqual(prs.get(1)["status"], "open")
        with self.assertRaises(prs.PRError):
            prs.close("default", 1, "  ")
        prs.close("default", 1, "想错了")
        self.assertEqual(prs.get(1)["status"], "closed")
        with self.assertRaises(prs.PRError):
            prs.close("default", 1, "再关一次")

    def test_list_unfinished_first(self):
        self.open_one("default", "a")
        self.open_one("default", "b")
        self.open_one("cass", "c")
        prs.close("default", 2, "不要了")
        order = [r["id"] for r in prs.list_prs()]
        self.assertEqual(order[-1], 2)
        self.assertEqual(set(order[:2]), {1, 3})

    def test_view_has_diff(self):
        self.open_one("default", "a", text="hello-diff\n")
        v = prs.view(1)
        self.assertIn("+hello-diff", v["diff"])
        with self.assertRaises(prs.PRError):
            prs.view(99)


class TestTests(PRBase):
    def test_new_failure_reported_baseline_ignored(self):
        prs.start("default", "a")
        r = prs.test("default")
        self.assertTrue(r["ok"], r["summary"])
        self.assertEqual(r["new_failed"], [])
        self.assertIn("本来就挂 1 条", r["summary"])
        self.edit("default", "server/mod.py", "VALUE = 2\n")
        r = prs.test("default")
        self.assertFalse(r["ok"])
        self.assertEqual(r["new_failed"], ["tests.test_mod.T.test_value"])

    def test_import_crash_counts_as_failure(self):
        prs.start("default", "a")
        self.edit("default", "server/mod.py", "VALUE = (\n")
        self.assertFalse(prs.test("default")["ok"])

    def test_no_worktree(self):
        with self.assertRaises(prs.PRError):
            prs.test("default")

    def test_sandbox_blocks_secrets_main_writes_and_network(self):
        (self.repo / "server" / ".env").write_text("SECRET=1\n")
        prs.start("default", "a")
        self.edit("default", "server/tests/test_probe.py", _PROBE.format(main=str(self.repo)))
        r = prs.test("default")
        self.assertTrue(r["ok"], r["summary"])
        self.assertEqual(r["ran"], 6)
        self.assertFalse((self.repo / "server" / "_pwn.py").exists())

    def test_no_sandbox_no_run(self):
        prs.start("default", "a")
        orig, prs.SANDBOX_EXEC = prs.SANDBOX_EXEC, "/nonexistent/sandbox-exec"
        try:
            r = prs.test("default")
        finally:
            prs.SANDBOX_EXEC = orig
        self.assertFalse(r["ok"])
        self.assertIn("沙箱", r["summary"])


class TestReplay(PRBase):
    def test_main_moved_clean_rebase(self):
        self.open_one("default", "a", text="a\nb\nc\nd\n")
        self.commit_on_main("server/mod.py", "VALUE = 1\nOTHER = 2\n")
        r = prs.start("default", "a")
        self.assertIn("重放", r["note"])
        self.assertEqual(_g(self.repo, "merge-base", "main", "wake/default/a"),
                         _g(self.repo, "rev-parse", "main"))

    def test_main_moved_with_uncommitted_edits_keeps_them(self):
        self.open_one("default", "a", text="mine\n")
        self.edit("default", "server/notes.txt", "unsaved\n")
        self.commit_on_main("server/notes.txt", "theirs\n")
        r = prs.start("default", "a")
        self.assertIn("先不重放", r["note"])
        self.assertEqual((self.wt("default") / "server/notes.txt").read_text(), "unsaved\n")

    def test_main_moved_conflict_leaves_markers(self):
        self.open_one("default", "a", text="mine\n")
        self.commit_on_main("server/notes.txt", "theirs\n")
        r = prs.start("default", "a")
        self.assertIn("冲突标记", r["note"])
        with self.assertRaises(prs.PRError):
            prs.open_pr("default", "t", "d")
        self.edit("default", "server/notes.txt", "merged\n")
        r = prs.open_pr("default", "t", "d")
        self.assertEqual(r["id"], 1)
        self.assertEqual(_g(self.repo, "merge-base", "main", "wake/default/a"),
                         _g(self.repo, "rev-parse", "main"))
        self.assertIn("+merged", prs.view(1)["diff"])


if __name__ == "__main__":
    unittest.main()


class TestMerge(PRBase):
    """§6.1：合并在主目录本地做；挡住的原因给机主看，跟 main 打架的还要通知他。"""

    def test_merge_ok_and_event_once(self):
        self.open_one("default", "a", text="merged-text\n")
        r = prs.merge(1)
        self.assertEqual(r["status"], "merged")
        self.assertEqual((self.repo / "server/notes.txt").read_text(), "merged-text\n")
        parents = _g(self.repo, "log", "-1", "--format=%P").split()
        self.assertEqual(len(parents), 2)                       # --no-ff
        self.assertTrue(prs._branch_exists("wake/default/a"))   # 验活之前分支不删
        self.assertEqual(prs.take_events("default"), ["#1「改 a」已合并。"])
        self.assertEqual(prs.take_events("default"), [])
        self.assertEqual(prs.take_events("cass"), [])

    def test_effective_follows_live_head(self):
        self.open_one("default", "a")
        self.assertIsNone(prs.effective(prs.get(1)))            # 没合
        prs.record_live_head()                                   # 后端跑的是合并前
        prs.merge(1)
        self.assertIs(prs.effective(prs.get(1)), False)
        self.assertIs(prs.list_prs()[0]["effective"], False)
        prs.record_live_head()                                   # 重启过
        self.assertIs(prs.view(1)["effective"], True)

    def test_conflict_refused_and_char_told(self):
        self.open_one("default", "a", text="mine\n")
        self.commit_on_main("server/notes.txt", "theirs\n")
        self.assertTrue(any("打架" in b for b in prs.merge_blockers(1)))
        with self.assertRaises(prs.PRError):
            prs.merge(1)
        self.assertEqual(prs.get(1)["status"], "open")
        ev = prs.take_events("default")
        self.assertEqual(len(ev), 1)
        self.assertIn("pr_start", ev[0])

    def test_dirty_overlap_refused_disjoint_ok(self):
        self.open_one("default", "a")
        (self.repo / "server/notes.txt").write_text("owner wip\n")
        with self.assertRaises(prs.PRError) as cm:
            prs.merge(1)
        self.assertIn("server/notes.txt", str(cm.exception))
        _g(self.repo, "checkout", "--", "server/notes.txt")
        (self.repo / "server/mod.py").write_text("VALUE = 1  # owner wip\n")
        prs.merge(1)
        self.assertEqual((self.repo / "server/mod.py").read_text(), "VALUE = 1  # owner wip\n")
        self.assertNotIn("owner wip", _g(self.repo, "show", "HEAD:server/mod.py"))

    def test_staged_or_off_main_refused(self):
        self.open_one("default", "a")
        (self.repo / "server/other.txt").write_text("x\n")
        _g(self.repo, "add", "server/other.txt")
        self.assertTrue(any("暂存区" in b for b in prs.merge_blockers(1)))
        _g(self.repo, "reset", "-q")
        _g(self.repo, "checkout", "-q", "-b", "owner-branch")
        self.assertTrue(any("不在 main" in b for b in prs.merge_blockers(1)))
        with self.assertRaises(prs.PRError):
            prs.merge(1)
        _g(self.repo, "checkout", "-q", "main")
        self.assertEqual(prs.merge_blockers(1), [])

    def test_failing_tests_need_force(self):
        r = self.open_one("default", "a", rel="server/mod.py", text="VALUE = 2\n")
        self.assertFalse(r["tests_ok"])
        with self.assertRaises(prs.NeedsForce) as cm:
            prs.merge(1)
        self.assertIn("新挂 1 条", str(cm.exception))
        self.assertEqual(prs.get(1)["status"], "open")
        prs.merge(1, force=True)
        self.assertTrue(prs.get(1)["forced"])

    def test_merge_only_open(self):
        self.open_one("default", "a")
        prs.close("default", 1, "不要了")
        with self.assertRaises(prs.PRError):
            prs.merge(1)
        self.assertEqual(prs.take_events("default"), [])         # 自己关的不通知自己

    def test_owner_close_any_char_and_told(self):
        self.open_one("cass", "a")
        prs.close_by_owner(1, "方向不对")
        rec = prs.get(1)
        self.assertEqual((rec["status"], rec["closed_by"]), ("closed", "owner"))
        ev = prs.take_events("cass")
        self.assertEqual(len(ev), 1)
        self.assertIn("方向不对", ev[0])
        with self.assertRaises(prs.PRError):
            prs.close_by_owner(1)


class TestRoutes(PRBase):
    """REST（app 调）：鉴权、列表 unapplied、详情 blockers、409 形状。"""

    def setUp(self):
        super().setUp()
        import config
        from fastapi.testclient import TestClient
        import app as app_module
        self._key = config.AUTH_KEY
        config.AUTH_KEY = "test-key"
        self.client = TestClient(app_module.app)
        self.h = {"X-Auth": "test-key"}

    def tearDown(self):
        import config
        config.AUTH_KEY = self._key
        super().tearDown()

    def test_auth_required(self):
        self.assertEqual(self.client.get("/prs").status_code, 401)

    def test_list_detail_merge_close(self):
        self.open_one("default", "a")
        self.open_one("cass", "b", rel="server/mod.py", text="VALUE = 2\n")
        prs.record_live_head()
        rows = self.client.get("/prs", headers=self.h).json()
        self.assertEqual({r["id"] for r in rows["prs"]}, {1, 2})
        self.assertTrue(all(r["char_name"] for r in rows["prs"]))
        d = self.client.get("/prs/1", headers=self.h).json()
        self.assertEqual(d["blockers"], [])
        self.assertIn("diff", d)
        self.assertEqual(self.client.get("/prs/99", headers=self.h).status_code, 404)

        self.assertEqual(self.client.post("/prs/1/merge", json={}, headers=self.h).status_code,
                         200)
        self.assertEqual(self.client.get("/prs", headers=self.h).json()["unapplied"], 1)

        r = self.client.post("/prs/2/merge", json={}, headers=self.h)
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["detail"]["needs_force"])
        r = self.client.post("/prs/2/close", json={"reason": "先不要"}, headers=self.h)
        self.assertEqual(r.json()["status"], "closed")
        r = self.client.post("/prs/2/close", json={}, headers=self.h)
        self.assertEqual(r.status_code, 409)
        self.assertIn("error", r.json()["detail"])
