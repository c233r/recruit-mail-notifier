# -*- coding: utf-8 -*-
"""一次执行模式：运行一次就把上次拉取之后的邮件补上、同步数据库并推送。"""

import logging
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from recruit_mail_notifier.config import load_config
from recruit_mail_notifier.mail_reader import RawMail, build_search_criteria
from recruit_mail_notifier.notifier import FileNotifier
from recruit_mail_notifier.pipeline import Pipeline


def _raw_mail(uid, subject, message_id, received_at=None):
    return RawMail(
        uid=uid,
        uid_validity=1,
        message_id=message_id,
        subject=subject,
        sender="hr@example.com",
        recipients="me@example.com",
        date_header="",
        received_at=received_at,
        body_text="面试时间：2026-03-08 14:30",
        mail_link="mid:%s" % message_id,
        mailbox="INBOX",
        account="test@example.com",
    )


class FakeReader:
    """假 IMAP：返回给定邮件，并记下调用参数，方便验证时间点有没有传进去。"""

    def __init__(self, mails):
        self.mails = mails
        self.calls = []

    def fetch_new(self, last_uid=None, uid_validity=None, last_fetch_at=None):
        self.calls.append({"last_uid": last_uid, "last_fetch_at": last_fetch_at})
        top = max([last_uid or 0] + [m.uid for m in self.mails])
        return 111, list(self.mails), top

    def close(self):
        pass


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)

        self.cfg = load_config()
        self.cfg.imap.username = "test@example.com"
        self.cfg.imap.password = "dummy"
        self.cfg.notify.recipients = ["me@example.com"]
        # 走规则快判，测试不联网、不调模型
        self.cfg.classifier.strategy = "rules_only"
        self.cfg.classifier.rule_scope = "subject"
        self.cfg.model.enabled = False
        self.cfg.retention.max_age_days = 0
        self.cfg.notify.poll_before_summary = False
        self.cfg.db_path = tmp / "test.db"
        self.cfg.log_dir = tmp / "logs"

        self.reader = FakeReader([_raw_mail(1, "【某公司】面试通知", "<a@x>")])
        self.pipeline = Pipeline(
            config=self.cfg,
            logger=logging.getLogger("test"),
            reader_factory=lambda cfg: self.reader,
            notifier=FileNotifier(tmp / "outbox"),
        )
        self.pipeline.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------ 一次执行
    def test_run_once_fetches_stores_and_pushes(self):
        result = self.pipeline.run_once()

        self.assertTrue(result["poll"]["ok"], result["poll"].get("note"))
        self.assertEqual(result["poll"]["inserted"], 1)
        self.assertEqual(result["summary"]["notified"], 1)
        row = self.pipeline.db.get_message_by_message_id("<a@x>")
        self.assertEqual(row["status"], "notified")

    def test_run_once_records_last_fetch_and_push_time(self):
        self.pipeline.run_once()
        db = self.pipeline.db
        self.assertIsNotNone(db.get_meta("last_fetch_at"), "要记下这次拉到哪儿了")
        self.assertIsNotNone(db.get_meta("last_push_at"))
        self.assertIsNotNone(db.get_meta("last_run_at"))

    def test_second_run_without_changes_skips_push(self):
        self.pipeline.run_once()
        self.reader.mails = []          # 第二次运行没有新邮件

        second = self.pipeline.run_once()

        self.assertTrue(second["summary"].get("skipped"), second["summary"])
        self.assertIn("没有变化", second["summary"].get("note", ""))

    def test_push_only_when_changed_can_be_disabled(self):
        self.cfg.notify.push_only_when_changed = False
        self.pipeline.run_once()
        self.reader.mails = []

        second = self.pipeline.run_once()

        # 关掉保护后，未完成项每次运行都会再推一遍
        self.assertFalse(second["summary"].get("skipped"))
        self.assertEqual(second["summary"]["notified"], 1)

    def test_new_mail_triggers_another_push(self):
        self.pipeline.run_once()
        self.reader.mails = [_raw_mail(2, "【另一家】笔试通知", "<b@x>")]

        second = self.pipeline.run_once()

        self.assertFalse(second["summary"].get("skipped"))
        self.assertEqual(second["summary"]["notified"], 2, "新旧两封未完成项都要推")

    def test_last_fetch_time_is_passed_to_reader(self):
        self.pipeline.db.set_meta("last_fetch_at", "2026-09-29T00:00:00+00:00")
        self.pipeline.run_once()
        passed = self.reader.calls[0]["last_fetch_at"]
        self.assertIsNotNone(passed, "要把上次拉取的时间点传给 reader")
        self.assertEqual(passed.day, 29)

    def test_change_detection_survives_same_second_events(self):
        """同一秒内的「推送 -> 标记完成」也必须算有变化（靠微秒精度）。"""
        self.pipeline.run_once()
        db = self.pipeline.db
        mark = db.get_meta("last_push_at")
        self.assertIsNotNone(mark)

        row = db.get_message_by_message_id("<a@x>")
        db.mark_completed([row["id"]], by="cli")

        self.assertTrue(
            db.has_summaryable_changes_since(mark),
            "刚标记完成应该被识别为有变化，否则改了状态却不会再推送",
        )

    def test_change_detection_false_when_nothing_happened(self):
        self.pipeline.run_once()
        db = self.pipeline.db
        mark = db.get_meta("last_push_at")
        self.assertFalse(db.has_summaryable_changes_since(mark))

    # ------------------------------------------------------------ SEARCH 条件
    def test_uid_mode_uses_uid_even_if_time_is_known(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        criteria = build_search_criteria(
            3237, False, 7, 30, now,
            last_fetch_at=datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc),
            fetch_mode="uid",
        )
        self.assertEqual(criteria[:2], ["UID", "3238:*"])

    def test_since_mode_uses_last_fetch_time(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        criteria = build_search_criteria(
            3237, False, 7, 30, now,
            last_fetch_at=datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc),
            fetch_mode="since",
        )
        self.assertEqual(criteria[0], "SINCE")
        self.assertIn("28-Sep-2026", criteria)   # SINCE 只到天，往前多算一天
        self.assertNotIn("UID", criteria)

    def test_first_run_without_cursor_uses_last_fetch_time(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        criteria = build_search_criteria(
            None, True, 7, 30, now,
            last_fetch_at=datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc),
            fetch_mode="uid",
        )
        self.assertIn("28-Sep-2026", criteria)

    def test_first_run_ever_uses_lookback(self):
        now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        criteria = build_search_criteria(None, True, 7, 30, now)
        self.assertIn("24-Sep-2026", criteria)

    # ------------------------------------------------------------ meta
    def test_meta_roundtrip(self):
        db = self.pipeline.db
        self.assertIsNone(db.get_meta("nope"))
        db.set_meta("k", "v1")
        self.assertEqual(db.get_meta("k"), "v1")
        db.set_meta("k", "v2")
        self.assertEqual(db.get_meta("k"), "v2")


if __name__ == "__main__":
    unittest.main()
