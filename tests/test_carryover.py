# -*- coding: utf-8 -*-
"""「完成回复 + 每日续推 + 截止时间排序」的离线端到端测试。"""

import logging
import tempfile
import unittest
from pathlib import Path

from recruit_mail_notifier.config import load_config
from recruit_mail_notifier.notifier import FileNotifier
from recruit_mail_notifier.pipeline import Pipeline
from tests import sample_replies as sr
from tests.sample_emails import build_sample_mails


class FakeReader:
    """假 IMAP：把给定邮件当作「本轮新邮件」。"""

    def __init__(self, mails):
        self.mails = mails

    def fetch_new(self, last_uid=None, uid_validity=None, last_fetch_at=None):
        if not self.mails:
            return 111, [], last_uid or 0
        return 111, list(self.mails), max(m.uid for m in self.mails)

    def close(self):
        pass


class CarryOverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)

        self.cfg = load_config()
        self.cfg.imap.username = "test@example.com"
        self.cfg.notify.recipients = ["me@example.com"]
        self.cfg.classifier.strategy = "rules_only"
        self.cfg.model.enabled = False
        # 单元测试里不真的去连 IMAP；需要验证「汇总前先收信」的用例会单独打开
        self.cfg.notify.poll_before_summary = False
        # 样例邮件用的是固定历史日期，关掉保留期过滤，否则会被当成超期跳过
        self.cfg.retention.max_age_days = 0
        # 这里测的是「续推」，与「只在有变化时推送」的保护无关，关掉后者
        self.cfg.notify.push_only_when_changed = False
        self.cfg.db_path = tmp_path / "test.db"
        self.cfg.log_dir = tmp_path / "logs"

        self.outbox = tmp_path / "outbox"
        self.pipeline = Pipeline(
            config=self.cfg, logger=logging.getLogger("test"), notifier=FileNotifier(self.outbox)
        )
        self.pipeline.init_db()
        self.samples = {sample["name"]: mail for sample, mail in build_sample_mails(self.cfg)}

    def tearDown(self):
        self.tmp.cleanup()

    def _latest_summary(self):
        files = sorted(self.outbox.glob("*-summary.txt"))
        return files[-1].read_text(encoding="utf-8") if files else ""

    def _ingest_samples(self):
        self.pipeline.ingest_mails(list(self.samples.values()))

    def test_deadline_is_filled_and_sorted(self):
        self._ingest_samples()
        rows = self.pipeline.db.list_pending_jobs(self.cfg.notify.categories)
        self.assertEqual(len(rows), 3)
        deadlines = [row["deadline"] for row in rows]
        self.assertTrue(all(deadlines), "每一条都应有截止时间（没有明确截止时用面试/笔试时间兜底）")
        self.assertEqual(deadlines, sorted(deadlines), "应按截止时间由近到远排序")
        # 腾讯 3/8 面试，字节 3/10 笔试，华为 3/12 面试
        self.assertIn("腾讯", rows[0]["company"] or "")
        self.assertIn("字节", rows[1]["company"] or "")

    def test_reply_marks_done_and_unfinished_carry_over(self):
        self._ingest_samples()
        first = self.pipeline.run_summary()
        self.assertTrue(first["ok"])
        self.assertEqual(first["notified"], 3)
        self.assertIn("腾讯", self._latest_summary())

        # 回复「腾讯」表示这家已经完成
        reply = sr.build_reply_mail(self.cfg, "腾讯\n\n在 2026年3月5日 10:05，助手 写道：\n" + sr.ORIGINAL_SUMMARY_BODY)
        self.pipeline.ingest_mails([reply])

        tencent = self.pipeline.db.get_message_by_message_id("<tencent-interview-001@tencent.com>")
        self.assertIsNotNone(tencent["completed_at"], "腾讯应被标记为已完成")
        self.assertEqual(tencent["status"], "done")
        self.assertEqual(tencent["completed_by"], "reply")

        # 第二天的汇总：腾讯不再出现，字节和华为继续推送
        second = self.pipeline.run_summary()
        self.assertTrue(second["ok"])
        self.assertEqual(second["notified"], 2, "未完成项第二天应继续推送")
        content = self._latest_summary()
        self.assertNotIn("腾讯", content)
        self.assertIn("字节跳动", content)
        self.assertIn("第 2 次提醒", content)

    def test_summary_polls_first_so_fresh_reply_takes_effect(self):
        """复现「回复之后直接跑汇总，结果没更新」：汇总前必须先收信。"""
        self._ingest_samples()
        first = self.pipeline.run_summary()
        self.assertEqual(first["notified"], 3)

        reply = sr.build_reply_mail(
            self.cfg, sr.QQ_REPLY.replace("广东电信", "腾讯"),
            message_id="<reply-sync@example.com>",
        )
        self.pipeline.reader_factory = lambda cfg: FakeReader([reply])
        self.cfg.imap.password = "dummy"
        self.cfg.notify.poll_before_summary = True

        second = self.pipeline.run_summary()

        tencent = self.pipeline.db.get_message_by_message_id("<tencent-interview-001@tencent.com>")
        self.assertIsNotNone(tencent["completed_at"], "汇总前应先收信并把回复应用上")
        self.assertEqual(second["notified"], 2, "已完成的腾讯不应再出现在汇总里")
        self.assertNotIn("腾讯", self._latest_summary())

    def test_reply_by_id_leaves_the_other_record_alone(self):
        """同一家公司有多条记录时，按编号标记只应标掉指定的那一条。"""
        self._ingest_samples()
        db = self.pipeline.db
        db.insert_message({
            "message_id": "<tencent-test@x>",
            "subject": "腾讯在线笔试通知",
            "company": "腾讯科技有限公司",
            "category": "written_test",
            "category_label": "笔试通知",
            "status": "notified",
            "received_at": "2026-03-03T00:00:00+00:00",
        })
        first = db.get_message_by_message_id("<tencent-interview-001@tencent.com>")

        reply = sr.build_reply_mail(
            self.cfg, "#%d 已完成\n" % first["id"], message_id="<reply-by-id@example.com>"
        )
        self.pipeline.ingest_mails([reply])

        done = db.get_message_by_message_id("<tencent-interview-001@tencent.com>")
        other = db.get_message_by_message_id("<tencent-test@x>")
        self.assertIsNotNone(done["completed_at"])
        self.assertIsNone(
            other["completed_at"], "按编号标记时不该连带标记同公司的其他记录"
        )

    def test_reply_by_company_still_marks_all_of_that_company(self):
        self._ingest_samples()
        db = self.pipeline.db
        db.insert_message({
            "message_id": "<tencent-test@x>",
            "subject": "腾讯在线笔试通知",
            "company": "腾讯科技有限公司",
            "category": "written_test",
            "category_label": "笔试通知",
            "status": "notified",
            "received_at": "2026-03-03T00:00:00+00:00",
        })

        reply = sr.build_reply_mail(
            self.cfg, "腾讯 已完成\n", message_id="<reply-by-name@example.com>"
        )
        self.pipeline.ingest_mails([reply])

        for message_id in ("<tencent-interview-001@tencent.com>", "<tencent-test@x>"):
            row = db.get_message_by_message_id(message_id)
            self.assertIsNotNone(row["completed_at"], "按公司名标记会标掉这家公司的全部记录")

    def test_own_summary_email_is_skipped(self):
        own = sr.build_reply_mail(
            self.cfg, sr.ORIGINAL_SUMMARY_BODY,
            subject=sr.OWN_SUMMARY_SUBJECT,
            message_id="<own-summary@example.com>",
            in_reply_to="",
        )
        ok, is_new = self.pipeline.store_mail(own)
        self.assertTrue(ok)
        self.assertFalse(is_new)
        self.assertIsNone(
            self.pipeline.db.get_message_by_message_id("<own-summary@example.com>"),
            "系统自己发的汇总不应入库",
        )

    def test_quoted_only_reply_completes_nothing(self):
        self._ingest_samples()
        self.pipeline.run_summary()
        reply = sr.build_reply_mail(self.cfg, sr.REPLY_ONLY_QUOTED, message_id="<reply-quoted@example.com>")
        self.pipeline.ingest_mails([reply])
        still_pending = self.pipeline.db.list_pending_jobs(self.cfg.notify.categories)
        self.assertEqual(len(still_pending), 3, "只有引用原文的回复不能标记任何记录")

    def test_reply_all_clears_everything(self):
        self._ingest_samples()
        self.pipeline.run_summary()
        reply = sr.build_reply_mail(self.cfg, "全部完成\n", message_id="<reply-all@example.com>")
        self.pipeline.ingest_mails([reply])
        self.assertEqual(self.pipeline.db.list_pending_jobs(self.cfg.notify.categories), [])
        skipped = self.pipeline.run_summary()
        self.assertTrue(skipped.get("skipped"))

    def test_undo_brings_item_back(self):
        self._ingest_samples()
        self.pipeline.run_summary()
        reply = sr.build_reply_mail(self.cfg, "腾讯\n", message_id="<reply-undo@example.com>")
        self.pipeline.ingest_mails([reply])
        row = self.pipeline.db.get_message_by_message_id("<tencent-interview-001@tencent.com>")
        self.pipeline.db.mark_undone([row["id"]])
        again = self.pipeline.db.get_message_by_message_id("<tencent-interview-001@tencent.com>")
        self.assertIsNone(again["completed_at"])
        self.assertEqual(again["status"], "notified")
        self.assertEqual(len(self.pipeline.db.list_pending_jobs(self.cfg.notify.categories)), 3)

    def test_reply_is_not_classified_as_job_mail(self):
        self._ingest_samples()
        self.pipeline.run_summary()
        reply = sr.build_reply_mail(self.cfg, "腾讯\n", message_id="<reply-cat@example.com>")
        self.pipeline.ingest_mails([reply])
        row = self.pipeline.db.get_message_by_message_id("<reply-cat@example.com>")
        self.assertEqual(row["category"], "reply")
        self.assertEqual(row["status"], "ignored")

    def test_reply_from_stranger_is_ignored(self):
        self._ingest_samples()
        self.pipeline.run_summary()
        reply = sr.build_reply_mail(
            self.cfg, "全部完成\n", sender="stranger@example.com", message_id="<reply-stranger@example.com>"
        )
        self.pipeline.ingest_mails([reply])
        self.assertEqual(len(self.pipeline.db.list_pending_jobs(self.cfg.notify.categories)), 3)


if __name__ == "__main__":
    unittest.main()
