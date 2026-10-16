# -*- coding: utf-8 -*-
"""邮件保留期测试：超期邮件不处理、启动清理旧记录、游标照常推进。"""

import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recruit_mail_notifier.config import load_config
from recruit_mail_notifier.mail_reader import build_search_criteria
from recruit_mail_notifier.mail_reader import RawMail
from recruit_mail_notifier.notifier import FileNotifier
from recruit_mail_notifier.pipeline import Pipeline


def _raw_mail(uid, received_at, subject, message_id):
    return RawMail(
        uid=uid,
        uid_validity=1,
        message_id=message_id,
        subject=subject,
        sender="hr@example.com",
        recipients="me@example.com",
        date_header="",
        received_at=received_at,
        body_text="面试时间：2026-09-29 10:00",
        mail_link="mid:%s" % message_id,
        mailbox="INBOX",
        account="test@example.com",
    )


def _record(message_id, received_at, subject="面试邀请", status="new"):
    return {
        "message_id": message_id,
        "account": "test@example.com",
        "mailbox": "INBOX",
        "uid_validity": 1,
        "uid": 1,
        "subject": subject,
        "sender": "hr@example.com",
        "recipients": "me@example.com",
        "category": "interview",
        "category_label": "面试通知",
        "company": "某某科技",
        "notify_time": received_at,
        "event_time": None,
        "event_time_raw": None,
        "deadline": None,
        "mail_link": "mid:x",
        "summary": "摘要",
        "confidence": 0.9,
        "source": "rules",
        "status": status,
        "reasons": [],
        "received_at": received_at,
    }


def _days_ago(days):
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.replace(microsecond=0).isoformat()


class FakeReader:
    def __init__(self, mails):
        self.mails = mails

    def fetch_new(self, last_uid=None, uid_validity=None, last_fetch_at=None):
        return 111, list(self.mails), max(m.uid for m in self.mails)

    def close(self):
        pass


class RetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)

        self.cfg = load_config()
        self.cfg.imap.username = "test@example.com"
        self.cfg.imap.password = "dummy"
        self.cfg.classifier.rule_scope = "subject"
        self.cfg.model.enabled = False
        self.cfg.db_path = tmp / "test.db"
        self.cfg.log_dir = tmp / "logs"
        self.cfg.retention.max_age_days = 30
        self.cfg.retention.purge_after_days = 30

        self.pipeline = Pipeline(
            config=self.cfg,
            logger=logging.getLogger("test"),
            notifier=FileNotifier(tmp / "outbox"),
        )
        self.pipeline.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------ 配置
    def test_config_is_loaded_from_mail_ini(self):
        cfg = load_config()
        self.assertEqual(cfg.retention.max_age_days, 30)
        self.assertEqual(cfg.retention.purge_after_days, 30)
        self.assertTrue(cfg.retention.purge_on_start)
        # IMAP 侧要拿到同一个值，才能在服务器端过滤
        self.assertEqual(cfg.imap.max_age_days, 30)

    # ------------------------------------------------------------ 超期不处理
    def test_old_mail_skipped_but_cursor_still_advances(self):
        ancient = _raw_mail(2001, _days_ago(120), "某公司面试通知", "<ancient@x>")
        recent = _raw_mail(2002, _days_ago(1), "小米集团测评邀请", "<recent@x>")
        self.pipeline.reader_factory = lambda cfg: FakeReader([ancient, recent])

        result = self.pipeline.run_poll()

        self.assertTrue(result["ok"], result.get("note"))
        self.assertEqual(result["skipped_old"], 1)
        self.assertIsNone(
            self.pipeline.db.get_message_by_message_id("<ancient@x>"),
            "超过保留期的邮件不应入库",
        )
        self.assertIsNotNone(self.pipeline.db.get_message_by_message_id("<recent@x>"))
        cursor = self.pipeline.db.get_cursor(self.cfg.imap.account, self.cfg.imap.mailbox)
        self.assertEqual(cursor["last_uid"], 2002, "跳过的老邮件也必须推进游标，否则会反复拉取")

    def test_age_filter_can_be_disabled(self):
        self.cfg.retention.max_age_days = 0
        ancient = _raw_mail(2001, _days_ago(2000), "某公司面试通知", "<ancient@x>")
        self.pipeline.reader_factory = lambda cfg: FakeReader([ancient])

        result = self.pipeline.run_poll()

        self.assertEqual(result["skipped_old"], 0)
        self.assertIsNotNone(self.pipeline.db.get_message_by_message_id("<ancient@x>"))

    def test_is_too_old_helper(self):
        self.assertTrue(self.pipeline._is_too_old(_raw_mail(1, _days_ago(60), "x", "<a@x>")))
        self.assertFalse(self.pipeline._is_too_old(_raw_mail(1, _days_ago(1), "x", "<a@x>")))
        # 没有时间信息时不拦（交给别的逻辑处理）
        self.assertFalse(self.pipeline._is_too_old(_raw_mail(1, None, "x", "<a@x>")))

    # ------------------------------------------------------------ 清理
    def test_purge_removes_only_old_records(self):
        db = self.pipeline.db
        db.insert_message(_record("<fresh@x>", _days_ago(3)))
        # 用 ignored 状态，表示已经处理完、可以清掉
        db.insert_message(_record("<stale@x>", _days_ago(45), status="ignored"))
        db.record_notify("b-old", ["me@x.com"], "旧汇总", 1, True, None, message_id="<m@x>")

        result = self.pipeline.run_purge()

        self.assertTrue(result["ok"], result.get("note"))
        self.assertEqual(result["messages"], 1)
        self.assertIsNone(db.get_message_by_message_id("<stale@x>"))
        self.assertIsNotNone(db.get_message_by_message_id("<fresh@x>"))

    def test_purge_protects_unnotified_by_default(self):
        db = self.pipeline.db
        # status=new：还没通知过的待办，默认不能删
        db.insert_message(_record("<stale@x>", _days_ago(45)))
        result = self.pipeline.run_purge()
        self.assertEqual(result["messages"], 0)
        self.assertEqual(result["protected"], 1)
        self.assertIsNotNone(db.get_message_by_message_id("<stale@x>"))

    def test_purge_protects_pending_review(self):
        db = self.pipeline.db
        db.insert_message(_record("<review@x>", _days_ago(45), status="pending_review"))
        result = self.pipeline.run_purge()
        self.assertEqual(result["messages"], 0)
        self.assertIsNotNone(db.get_message_by_message_id("<review@x>"))

    def test_purge_force_removes_unnotified(self):
        db = self.pipeline.db
        db.insert_message(_record("<stale@x>", _days_ago(45)))
        result = self.pipeline.run_purge(force=True)
        self.assertEqual(result["messages"], 1)
        self.assertEqual(result["keep_unnotified"], False)
        self.assertIsNone(db.get_message_by_message_id("<stale@x>"))

    def test_purge_disabled_by_zero(self):
        self.cfg.retention.purge_after_days = 0
        result = self.pipeline.run_purge()
        self.assertTrue(result.get("skipped"))

    def test_purge_days_override(self):
        db = self.pipeline.db
        db.insert_message(_record("<mid@x>", _days_ago(10), status="ignored"))
        # 默认保留 30 天，不该删；显式指定 5 天就应该删掉
        self.assertEqual(self.pipeline.run_purge(days=5)["messages"], 1)
        self.assertIsNone(db.get_message_by_message_id("<mid@x>"))

class SearchCriteriaTest(unittest.TestCase):
    """回归：QQ 邮箱不接受裸序列集，增量拉取必须写成 `UID n:*`。"""

    def _now(self):
        return datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_incremental_search_uses_uid_keyword(self):
        criteria = build_search_criteria(3237, False, 7, 30, self._now())
        self.assertIn("UID", criteria, "必须带 UID 关键字")
        self.assertIn("3238:*", criteria)
        self.assertNotEqual(criteria[0], "3238:*", "裸序列集会被 QQ 静默忽略")

    def test_incremental_search_without_age_filter(self):
        criteria = build_search_criteria(100, False, 7, 0, self._now())
        self.assertEqual(criteria, ["UID", "101:*"])

    def test_first_run_uses_since_with_lookback(self):
        criteria = build_search_criteria(None, True, 7, 30, self._now())
        self.assertEqual(criteria[0], "SINCE")
        self.assertNotIn("UID", criteria)
        # 回看天数不能超过保留期
        self.assertIn("24-Sep-2026", criteria)

    def test_lookback_is_capped_by_max_age(self):
        criteria = build_search_criteria(None, True, 60, 30, self._now())
        self.assertIn("01-Sep-2026", criteria)


if __name__ == "__main__":
    unittest.main()