"""jobhunt_store（J0）单测。

全部跑在临时目录上——store 的路径用 _rebase 整体换掉、tearDown 换回来，
绝不写真 state/jobhunt/（那里面是真简历真台账）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_jobhunt -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobhunt_store as store


class JobhuntBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jobhunt_test_"))
        self._orig = store.JOB_DIR
        store._rebase(self.tmp / "jobhunt")

    def tearDown(self):
        store._rebase(self._orig)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestResume(JobhuntBase):
    def test_master_missing_hint(self):
        r = store.resume_read("master")
        self.assertTrue(r["missing"])

    def test_save_and_read(self):
        r = store.resume_save("# 名字\n内容", rid="master")
        self.assertTrue(r["ok"])
        self.assertIn("内容", store.resume_read("master")["content"])

    def test_variant_gets_frontmatter(self):
        store.resume_save("# 名字", rid="字节-iOS", jd_id="jd1", note="试试")
        c = store.resume_read("字节-iOS")["content"]
        self.assertTrue(c.startswith("<!-- jd_id: jd1"))

    def test_duplicate_rid_rejected(self):
        store.resume_save("v1", rid="AI全栈-通用版")
        r = store.resume_save("v2", rid="AI全栈-通用版")
        self.assertFalse(r["ok"])
        self.assertTrue(r["exists"])
        # 没被盖掉
        self.assertIn("v1", store.resume_read("AI全栈-通用版")["content"])
        # overwrite 明说才盖
        r = store.resume_save("v2", rid="AI全栈-通用版", overwrite=True)
        self.assertTrue(r["ok"])
        self.assertIn("v2", store.resume_read("AI全栈-通用版")["content"])

    def test_master_overwrite_also_guarded(self):
        store.resume_save("v1", rid="master")
        r = store.resume_save("v2", rid="master")
        self.assertFalse(r["ok"])

    def test_auto_name(self):
        r1 = store.resume_save("a")
        r2 = store.resume_save("b")
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertNotEqual(r1["id"], r2["id"])

    def test_rid_path_traversal_rejected(self):
        for bad in ("../x", "a/b", "a.b", "", "a\x00b"):
            with self.assertRaises(ValueError):
                store._variant_path(bad)

    def test_resume_list_master_first(self):
        store.resume_save("v", rid="某厂-前端")
        store.resume_save("m", rid="master")
        ids = [r["id"] for r in store.resume_list()]
        self.assertEqual(ids[0], "master")
        self.assertIn("某厂-前端", ids)


class TestMdToHtml(JobhuntBase):
    def test_comments_stripped(self):
        html = store.md_to_html("<!-- 改动记录 -->\n# 名字\n<!-- 第二块注记 -->\n正文")
        self.assertNotIn("改动记录", html)
        self.assertNotIn("注记", html)
        self.assertIn("正文", html)

    def test_date_tail_zh_and_en(self):
        # h1 后第一段是联系方式段（进 header），日期行得在它之后才走时间轴逻辑
        for tail in ("2024.09-至今", "2024.09-Present"):
            html = store.md_to_html(f"# 名\n\nmail@x.com\n\n**某厂** · {tail}")
            self.assertIn('class="d"', html)
            self.assertIn(tail.split("-")[1], html)

    def test_backtick_not_emphasis(self):
        html = store.md_to_html("# 名\n\n- 用 `a*b*c` 写的")
        self.assertIn("<code>a*b*c</code>", html)


class TestJdStateMachine(JobhuntBase):
    def _new_jd(self, company="某厂", title="iOS"):
        return store.jd_save("测试", company, title, "JD 全文", char_id="cass")["id"]

    def test_save_is_new(self):
        jid = self._new_jd()
        self.assertEqual(store.jd_read(jid)["status"], "new")

    def test_score_moves_to_scored(self):
        jid = self._new_jd()
        r = store.jd_score(jid, 80, "对口", char_id="cass")
        self.assertTrue(r["ok"])
        row = store.jd_read(jid)
        self.assertEqual((row["status"], row["score"], row["char_id"]),
                         ("scored", 80.0, "cass"))

    def test_double_score_rejected_with_who(self):
        jid = self._new_jd()
        store.jd_score(jid, 80, "对口", char_id="cass")
        r = store.jd_score(jid, 60, "一般", char_id="xiaoka")
        self.assertFalse(r["ok"])
        self.assertTrue(r["already"])
        self.assertIn("cass", r["hint"])
        self.assertIn("对口", r["hint"])
        # 没被改动
        self.assertEqual(store.jd_read(jid)["score"], 80.0)
        # override 明说才覆盖
        r = store.jd_score(jid, 60, "一般", char_id="xiaoka", override=True)
        self.assertTrue(r["ok"])
        self.assertEqual(store.jd_read(jid)["char_id"], "xiaoka")

    def test_archived_refuses_score(self):
        jid = self._new_jd()
        store.jd_archive(jid)
        r = store.jd_score(jid, 80, "x")
        self.assertFalse(r["ok"])
        self.assertIn("不投", r["error"])

    def test_illegal_transition_raises(self):
        jid = self._new_jd()
        with self.assertRaises(ValueError):
            store._jd_set_status(jid, "sent")   # new → sent 不许跳

    def test_draft_then_sent(self):
        jid = self._new_jd()
        store.jd_score(jid, 80, "对口")
        store._jd_set_status(jid, "drafted")
        store._jd_set_status(jid, "drafted")   # 草稿拟重复不算事故
        store._jd_set_status(jid, "sent")
        self.assertEqual(store.jd_read(jid)["status"], "sent")
        with self.assertRaises(ValueError):
            store.jd_archive(jid)              # 投出去的不能标不投

    def test_archive_unarchive(self):
        jid = self._new_jd()
        store.jd_score(jid, 70, "还行")
        store.jd_archive(jid)
        self.assertEqual(store.jd_read(jid)["status"], "archived")
        store.jd_unarchive(jid)
        self.assertEqual(store.jd_read(jid)["status"], "scored")
        # 没打过分的 unarchive 回 new
        jid2 = self._new_jd("别厂")
        store.jd_archive(jid2)
        store.jd_unarchive(jid2)
        self.assertEqual(store.jd_read(jid2)["status"], "new")

    def test_jd_list_filter_and_head(self):
        jid = self._new_jd()
        store.jd_save("测试", "厂2", "岗2", "x" * 500)
        store.jd_score(jid, 80, "对口")
        news = store.jd_list(status="new")
        self.assertEqual([r["company"] for r in news], ["厂2"])
        self.assertEqual(len(news[0]["text_head"]), 120)
        self.assertNotIn("text", news[0])


class TestApplications(JobhuntBase):
    def test_add_marks_jd_sent_and_reply_returns_handler(self):
        jid = store.jd_save("测试", "某厂", "iOS", "JD", char_id="cass")["id"]
        store.jd_score(jid, 80, "对口", char_id="xiaoka")
        store._jd_set_status(jid, "drafted")
        r = store.application_add("dr123", jid, "hr@x.com", "AI全栈-通用版",
                                  "<mid@163.com>", char_id="xiaoka", subject="应聘iOS")
        self.assertTrue(r["ok"])
        self.assertEqual(store.jd_read(jid)["status"], "sent")
        apps = store.applications_open()
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["company"], "某厂")
        self.assertEqual(apps[0]["char_id"], "xiaoka")
        # 回信标记：返回经手人（J3 靠它决定硬醒谁）
        r = store.applications_mark_reply("dr123", "hr@x.com", "面试邀请", "下周二方便吗")
        self.assertTrue(r["ok"])
        self.assertEqual(r["char_id"], "xiaoka")
        row = store.applications_list()[0]
        self.assertTrue(row["has_reply"])
        # 已回信的还在匹配集里（HR 会连着来第二封）
        self.assertEqual(len(store.applications_open()), 1)

    def test_add_without_jd(self):
        r = store.application_add("dr9", "", "hr@x.com", "master", "<m@x>", char_id="cass")
        self.assertTrue(r["ok"])
        self.assertEqual(store.applications_list()[0]["company"], "")


class TestDraftFlow(JobhuntBase):
    """J2：email_draft → 通道角色草稿信箱 → 机主点发送 → 台账 + jd→sent。
    SMTP/Bark 全 mock，角色 state 指临时区（草稿别落进真信箱）。"""

    def setUp(self):
        super().setUp()
        import state_store
        import mail_bridge
        self._csr = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        self.mb = mail_bridge

    def tearDown(self):
        import state_store
        state_store.CHAR_STATE_ROOT = self._csr
        super().tearDown()

    def _fake_pdf(self, rid="测试版"):
        (store.PDF_DIR / f"{rid}.pdf").write_bytes(b"%PDF-1.4 fake")
        return rid

    def test_draft_requires_rendered_pdf(self):
        with self.assertRaises(self.mb.MailError):
            self.mb.jobhunt_draft("hr@x.com", "应聘", "你好", "没渲染过的", by_char="cass")

    def test_draft_lands_in_channel_and_send_writes_ledger(self):
        from unittest import mock
        rid = self._fake_pdf()
        jid = store.jd_save("测试", "某厂", "iOS", "JD", char_id="cass")["id"]
        with mock.patch("notify.bark_push") as bark:
            r = self.mb.jobhunt_draft("hr@x.com", "应聘iOS", "您好", rid,
                                      jd_id=jid, by_char="cass")
        self.assertTrue(r["drafted"])
        self.assertEqual(store.jd_read(jid)["status"], "drafted")
        channel = store.channel_char()
        drafts = self.mb.drafts_list(channel)
        self.assertEqual(len(drafts), 1)
        d = drafts[0]
        self.assertEqual((d["origin"], d["resume_id"], d["char_id"], d["company"]),
                         ("jobhunt", rid, "cass", "某厂"))
        self.assertEqual(d["attach_name"], f"简历-{rid}.pdf")
        # Bark 是丢线程发的，等那口气
        import time
        for _ in range(50):
            if bark.called:
                break
            time.sleep(0.02)
        self.assertTrue(bark.called)
        # 机主点发送（mock 掉 SMTP 和邮箱配置）
        cfg = {"address": "ch@163.com", "auth_code": "x", "imap_host": "", "smtp_host": "",
               "allow_to": set(), "hourly_cap": 5}
        with mock.patch.object(self.mb, "_cfg", return_value=cfg), \
             mock.patch.object(self.mb, "_smtp_send", return_value="<mid@163.com>") as ss:
            res = self.mb.draft_send(d["id"], channel)
        self.assertTrue(res["sent"])
        # 附件路径是 server 侧现场解析的 PDF，附件名从草稿带
        _, kwargs = ss.call_args
        self.assertEqual(kwargs["attachment"], store.pdf_path(rid))
        self.assertEqual(kwargs["attach_name"], f"简历-{rid}.pdf")
        # 台账落了、经手人对、jd 走到 sent、草稿删了
        apps = store.applications_list()
        self.assertEqual(len(apps), 1)
        self.assertEqual((apps[0]["id"], apps[0]["char_id"], apps[0]["message_id"]),
                         (d["id"], "cass", "<mid@163.com>"))
        self.assertEqual(store.jd_read(jid)["status"], "sent")
        self.assertEqual(self.mb.drafts_list(channel), [])

    def test_send_refuses_when_pdf_gone(self):
        from unittest import mock
        rid = self._fake_pdf()
        with mock.patch("notify.bark_push"):
            self.mb.jobhunt_draft("hr@x.com", "应聘", "您好", rid, by_char="cass")
        (store.PDF_DIR / f"{rid}.pdf").unlink()
        channel = store.channel_char()
        d = self.mb.drafts_list(channel)[0]
        cfg = {"address": "ch@163.com", "auth_code": "x", "imap_host": "", "smtp_host": "",
               "allow_to": set(), "hourly_cap": 5}
        with mock.patch.object(self.mb, "_cfg", return_value=cfg):
            with self.assertRaises(self.mb.MailError):
                self.mb.draft_send(d["id"], channel)
        # 草稿还在（没发出去就不删）
        self.assertEqual(len(self.mb.drafts_list(channel)), 1)


class TestRender(JobhuntBase):
    @unittest.skipUnless(store._find_chrome(), "机器上没有 Chrome/Chromium")
    def test_render_pdf(self):
        store.resume_save("# 测试员\n\nmail@x.com · 138xxxx\n\n## 经历\n\n**某厂**·工程师 2024.09-至今\n- 干了活",
                          rid="master")
        r = store.resume_render("master", timeout=90)
        self.assertTrue(r["ok"], msg=r.get("error"))
        self.assertTrue(store.pdf_path("master").exists())


if __name__ == "__main__":
    unittest.main()
