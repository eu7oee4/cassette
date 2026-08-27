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


class TestMailClassify(JobhuntBase):
    """J3：watcher 三分类。IMAP 用假连接，硬醒用 mock——只验分类和路由逻辑。"""

    class FakeConn:
        def __init__(self, raw: bytes):
            self.raw = raw

        def uid(self, cmd, uid, spec):
            return ("OK", [(b"x", self.raw)])

    def setUp(self):
        super().setUp()
        import mail_bridge
        self.mb = mail_bridge

    def _msg(self, headers: dict):
        import email
        import email.policy
        raw = "".join(f"{k}: {v}\r\n" for k, v in headers.items()).encode()
        return email.message_from_bytes(raw + b"\r\n", policy=email.policy.compat32)

    def _jh(self):
        apps = store.applications_open()
        return {"store": store,
                "by_addr": {a["to"].lower(): a for a in apps if a.get("to")},
                "by_mid": {a["message_id"]: a for a in apps if a.get("message_id")},
                "companies": __import__("mail_bridge")._company_keys(store)}

    def _seed_app(self):
        store.application_add("dr1", "", "hr@x.com", "AI全栈-通用版",
                              "<mid123@163.com>", char_id="cass", subject="应聘iOS")

    def _classify(self, conn, uid, msg, addrs):
        jh = self._jh()
        v = self.mb._jobhunt_classify(conn, uid, msg, addrs, jh, "cass")
        if v is None:
            return None, None
        return v, self.mb._jobhunt_apply(conn, uid, v, jh, "cass")

    def test_reply_marks_ledger_and_flags_without_body(self):
        self._seed_app()
        conn = self.FakeConn(b"From: hr@x.com\r\nSubject: Re: hi\r\n\r\nnext tuesday ok?")
        msg = self._msg({"From": "hr@x.com", "Subject": "Re: hi"})
        v, flag = self._classify(conn, 5, msg, {"hr@x.com"})
        self.assertEqual(v["kind"], "reply")
        row = store.applications_list()[0]
        self.assertTrue(row["has_reply"])
        self.assertIn("tuesday", row["reply_snippet"])      # 摘要落台账
        self.assertIsNotNone(flag)
        self.assertNotIn("tuesday", flag["why"])            # 但不进醒来 prompt

    def test_reply_by_message_id_refs(self):
        self._seed_app()
        # HR 换了个地址回（求职者邮箱→个人邮箱），靠 In-Reply-To 命中
        conn = self.FakeConn(b"From: hr2@qq.com\r\nSubject: Re: x\r\n\r\nok")
        msg = self._msg({"From": "hr2@qq.com", "Subject": "Re: x",
                         "In-Reply-To": "<mid123@163.com>"})
        v, flag = self._classify(conn, 6, msg, {"hr2@qq.com"})
        self.assertEqual(v["kind"], "reply")
        self.assertTrue(store.applications_list()[0]["has_reply"])
        self.assertIsNotNone(flag)

    def test_recruit_mail_saved_not_flagged(self):
        conn = self.FakeConn(b"From: n@zhipin.com\r\nSubject: 3 jobs\r\n\r\njd body " + b"x" * 5000)
        msg = self._msg({"From": "n@zhipin.com", "Subject": "3 jobs"})
        v, flag = self._classify(conn, 7, msg, {"n@zhipin.com"})
        self.assertEqual(v["kind"], "subscribe")
        self.assertIsNone(flag)                             # 入库，不惊动谁
        jds = store.jd_list(status="new")
        self.assertEqual(len(jds), 1)
        self.assertIn("邮件订阅", jds[0]["source"])
        self.assertLessEqual(len(store.jd_read(jds[0]["id"])["text"]), 3000)

    def test_other_mail_untouched(self):
        conn = self.FakeConn(b"")
        msg = self._msg({"From": "friend@qq.com", "Subject": "hi"})
        self.assertIsNone(self.mb._jobhunt_classify(conn, 8, msg, {"friend@qq.com"},
                                                    self._jh(), "cass"))

    def test_watch_gate_only_channel_char(self):
        # default 不是通道角色（.env 钉的是 cass）→ 不挂分类
        self.assertIsNone(self.mb._jobhunt_watch("default"))

    # ---- lead 兜底（2026-08-27 加。起因：帆软笔试邀请 + 拓端邀约两头不沾，静默掉地上）----

    def _seed_jd(self, company, title="AI 实习"):
        store.jd_save(source="test", company=company, title=title, text="x")

    def test_lead_by_company_in_subject(self):
        self._seed_jd("杭州拓端数据科技有限公司")
        conn = self.FakeConn(b"")
        msg = self._msg({"From": "tecdat <contact@tecdat.cn>", "Subject": "邀请您加入拓端"})
        v, flag = self._classify(conn, 9, msg, {"contact@tecdat.cn"})
        self.assertEqual(v["kind"], "lead")
        self.assertIn("拓端", flag["why"])

    def test_lead_by_company_in_from_name(self):
        self._seed_jd("帆软软件 · 盘古实验室")
        conn = self.FakeConn(b"")
        msg = self._msg({"From": "帆软招聘 <HR@fanedm.fanruan.com>", "Subject": "一封通知"})
        v, flag = self._classify(conn, 10, msg, {"hr@fanedm.fanruan.com"})
        self.assertEqual(v["kind"], "lead")
        self.assertIn("帆软", flag["why"])

    def test_lead_by_hint_word(self):
        conn = self.FakeConn(b"")
        msg = self._msg({"From": "x@unknown-corp.com", "Subject": "邀请你参加在线笔试"})
        v, flag = self._classify(conn, 11, msg, {"x@unknown-corp.com"})
        self.assertEqual(v["kind"], "lead")
        self.assertIn("笔试", flag["why"])

    def test_lead_beats_subscribe(self):
        # 招聘站代发的**笔试通知**：先判 lead，别被域名表吞成静默入库
        self._seed_jd("帆软软件")
        conn = self.FakeConn(b"")
        msg = self._msg({"From": "帆软 <s@nowcoder.com>", "Subject": "邀请你参加在线笔试"})
        v, _ = self._classify(conn, 12, msg, {"s@nowcoder.com"})
        self.assertEqual(v["kind"], "lead")
        self.assertEqual([j for j in store.jd_list(status="new")
                          if "邮件订阅" in (j["source"] or "")], [])

    def test_marketing_mail_not_a_lead(self):
        # 猎聘群发广告：公司不在库里、主题没关键词 → 一个字都不该惊动谁
        self._seed_jd("杭州拓端数据科技有限公司")
        conn = self.FakeConn(b"")
        for subj in ("给未来 Makers 的一封信｜安克创新2027届全球校招正式启动｜邀请创造者投递！(AD)",
                     "对您很感兴趣，邀您沟通",
                     "杨竹琼同学，你有一份实习僧简历提升礼包待领取，请查收"):
            msg = self._msg({"From": "猎聘 <service@mail8.lietou-edm.com>", "Subject": subj})
            self.assertIsNone(
                self.mb._jobhunt_classify(conn, 13, msg, {"service@mail8.lietou-edm.com"},
                                          self._jh(), "cass"), subj)

    def test_company_keys_shape(self):
        for c in ("Ant Group（蚂蚁集团）· Ling Team 百灵",
                  "ALLTIME万物时（杭州西湖边，10-20人）",
                  "杭州拓端数据科技有限公司", "Intel CAIGC", "帆软软件 · 盘古实验室"):
            self._seed_jd(c)
        keys = self.mb._company_keys(store)
        self.assertIn("拓端", keys)
        self.assertIn("帆软", keys)
        self.assertIn("Intel", keys)
        self.assertIn("ALLTIME", keys)
        self.assertNotIn("Group", keys)     # 通用词绝不能留下来误伤每封英文邮件
        self.assertNotIn("Ant", keys)       # 3 字母，太短
        self.assertTrue(all(len(k) >= 2 for k in keys))



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
