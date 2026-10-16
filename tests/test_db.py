# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

from recruit_mail_notifier.db import Database, STATUS_DONE, STATUS_NEW, STATUS_NOTIFIED


def _record(message_id="<a@b>", **overrides):
    base = {
        "message_id": message_id,
        "account": "me@example.com",
        "mailbox": "INBOX",
        "uid_validity": 111,
        "uid": 1000,
        "subject": "面试邀请",
        "sender": "hr@x.com",
        "recipients": "me@example.com",
        "category": "interview",
        "category_label": "面试通知",
        "company": "某某科技",
        "notify_time": "2026-03-02T09:12:00+08:00",
        "event_time": "2026-03-08T14:30:00+08:00",
        "event_time_raw": "2026年3月8日 14:30",
        "mail_link": "mid:a@b",
        "summary": "面试时间 3月8日 14:30",
        "confidence": 0.9,
        "source": "rules",
        "status": STATUS_NEW,
        "reasons": ["命中强关键词：面试邀请"],
        "received_at": "2026-03-02T01:12:00+00:00",
    }
    base.update(overrides)
    return base


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        self.db.init_schema()

    def tearDown(self):
        self.tmp.cleanup()

    def test_message_id_dedup(self):
        inserted, first_id = self.db.insert_message(_record())
        self.assertTrue(inserted)
        inserted_again, second_id = self.db.insert_message(_record(company="改了也不该覆盖"))
        self.assertFalse(inserted_again, "同一个 message_id 不应重复插入")
        self.assertEqual(first_id, second_id)
        row = self.db.get_message_by_message_id("<a@b>")
        self.assertEqual(row["company"], "某某科技", "重复插入不应覆盖首次结果")
        self.assertEqual(self.db.stats()["total"], 1)

    def test_cursor_roundtrip(self):
        self.assertIsNone(self.db.get_cursor("me@example.com", "INBOX"))
        self.db.set_cursor("me@example.com", "INBOX", 111, 1000)
        row = self.db.get_cursor("me@example.com", "INBOX")
        self.assertEqual(row["last_uid"], 1000)
        self.assertEqual(row["uid_validity"], 111)
        self.db.set_cursor("me@example.com", "INBOX", 111, 1005)
        self.assertEqual(self.db.get_cursor("me@example.com", "INBOX")["last_uid"], 1005)

    def test_mark_notified_tracks_reminders(self):
        _, row_id = self.db.insert_message(_record())
        self.assertEqual(len(self.db.list_unnotified(["interview"])), 1)
        self.assertEqual(self.db.mark_notified([row_id], "batch-1"), 1)
        first = self.db.get_message_by_message_id("<a@b>")
        self.assertEqual(first["status"], STATUS_NOTIFIED)
        self.assertEqual(first["notify_batch"], "batch-1")
        self.assertEqual(first["reminder_count"], 1)

        # 未完成的记录会被反复提醒：只累加提醒次数，首次通知时间保持不变
        self.assertEqual(self.db.mark_notified([row_id], "batch-2"), 1)
        second = self.db.get_message_by_message_id("<a@b>")
        self.assertEqual(second["notified_at"], first["notified_at"])
        self.assertEqual(second["reminder_count"], 2)
        self.assertEqual(second["notify_batch"], "batch-2")

        # 不再算「尚未通知」，但仍是未完成，所以要继续被推送
        self.assertEqual(len(self.db.list_unnotified(["interview"])), 0)
        self.assertEqual(len(self.db.list_pending_jobs(["interview"])), 1)

    def test_completed_jobs_leave_the_reminder_list(self):
        _, row_id = self.db.insert_message(_record("<a@b>"))
        self.db.insert_message(_record("<c@d>", company="另一家"))
        self.assertEqual(len(self.db.list_pending_jobs(["interview"])), 2)
        self.db.mark_completed([row_id], by="cli")
        rows = self.db.list_pending_jobs(["interview"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], "<c@d>")
        row = self.db.get_message_by_message_id("<a@b>")
        self.assertEqual(row["status"], STATUS_DONE)
        self.assertEqual(row["completed_by"], "cli")

    def test_pending_jobs_sorted_by_deadline(self):
        self.db.insert_message(_record("<late@x>", deadline="2026-03-20T09:00:00+08:00"))
        self.db.insert_message(_record("<soon@x>", deadline="2026-03-06T09:00:00+08:00"))
        self.db.insert_message(_record("<none@x>", deadline=None))
        rows = self.db.list_pending_jobs(["interview"])
        self.assertEqual(
            [row["message_id"] for row in rows],
            ["<soon@x>", "<late@x>", "<none@x>"],
            "应按截止时间由近到远，没有截止时间的排最后",
        )

    def test_find_jobs_by_company(self):
        self.db.insert_message(_record("<a@b>", company="腾讯科技有限公司"))
        self.db.insert_message(_record("<c@d>", company="字节跳动"))
        found = self.db.find_jobs_by_company("腾讯", ["interview"])
        self.assertEqual([row["message_id"] for row in found], ["<a@b>"])

    def test_recent_notify_message_ids(self):
        self.db.record_notify("b1", ["me@x.com"], "汇总", 1, True, None, message_id="<m1@x>")
        self.db.record_notify("b2", ["me@x.com"], "汇总", 1, True, None, message_id="<m2@x>")
        self.db.record_notify("b3", ["me@x.com"], "失败", 0, False, "err")
        ids = self.db.recent_notify_message_ids()
        self.assertIn("<m1@x>", ids)
        self.assertIn("<m2@x>", ids)
        self.assertEqual(len(ids), 2)

    def test_list_unnotified_filters_by_category(self):
        self.db.insert_message(_record("<a@b>", category="interview"))
        self.db.insert_message(_record("<c@d>", category="offer"))
        self.assertEqual(len(self.db.list_unnotified(["interview"])), 1)
        self.assertEqual(len(self.db.list_unnotified(["interview", "offer"])), 2)

    def test_pending_review_listing(self):
        self.db.insert_message(_record("<a@b>", status="pending_review", confidence=0.3))
        self.db.insert_message(_record("<c@d>", status="new"))
        pending = self.db.list_pending_review()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["message_id"], "<a@b>")

    def test_runs_and_notify_log(self):
        run_id = self.db.start_run("poll")
        self.db.finish_run(run_id, True, fetched=3, inserted=2, errors=0, note="ok")
        self.db.record_notify("batch-1", ["me@example.com"], "汇总", 2, True, None)
        with self.db.session() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            log = conn.execute("SELECT * FROM notify_log").fetchone()
        self.assertEqual(run["fetched"], 3)
        self.assertEqual(run["ok"], 1)
        self.assertEqual(log["item_count"], 2)
        self.assertEqual(log["ok"], 1)

    def test_llm_usage_counter(self):
        self.assertEqual(self.db.llm_calls_today(), 0)
        self.db.bump_llm_calls()
        self.db.bump_llm_calls(amount=2)
        self.assertEqual(self.db.llm_calls_today(), 3)


if __name__ == "__main__":
    unittest.main()