# -*- coding: utf-8 -*-
"""离线端到端测试：样例邮件 -> 分类入库 -> 汇总发送 -> 标记已通知。

用 FileNotifier 代替真实 SMTP，所以整个流程不联网、也不会真的发信。
"""

import logging
import tempfile
import unittest
from pathlib import Path

from recruit_mail_notifier.config import load_config
from recruit_mail_notifier.notifier import FileNotifier
from recruit_mail_notifier.pipeline import Pipeline
from tests.sample_emails import build_sample_mails


class PipelineOfflineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)

        self.cfg = load_config()
        self.cfg.imap.username = "test@example.com"
        self.cfg.notify.recipients = ["me@example.com"]
        self.cfg.classifier.strategy = "rules_only"
        # 这里测的是管道机制（去重 / 续推 / 发送失败），固定成全文匹配，
        # 免得规则范围调整影响到这些断言；规则范围本身由 test_subject_rules 覆盖
        self.cfg.classifier.rule_scope = "subject_body"
        self.cfg.model.enabled = False
        # 单元测试里不连 IMAP
        self.cfg.notify.poll_before_summary = False
        # 这里测的是「未完成项续推」，与「只在有变化时推送」的保护无关，关掉后者
        self.cfg.notify.push_only_when_changed = False
        # 样例邮件用的是固定历史日期，这里关掉保留期过滤，
        # 否则会被全部当成「超过一个月」而跳过（保留期本身由 test_retention 覆盖）
        self.cfg.retention.max_age_days = 0
        self.cfg.db_path = tmp_path / "test.db"
        self.cfg.log_dir = tmp_path / "logs"

        self.outbox = tmp_path / "outbox"
        self.pipeline = Pipeline(
            config=self.cfg,
            logger=logging.getLogger("test"),
            notifier=FileNotifier(self.outbox),
        )
        self.pipeline.init_db()
        self.samples = build_sample_mails(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _ingest(self):
        return self.pipeline.ingest_mails([mail for _, mail in self.samples])

    def test_end_to_end_no_loss_no_duplicate(self):
        stats = self._ingest()
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["inserted"], len(self.samples))

        # 再导入一次：唯一 ID 去重，不应产生新记录
        again = self._ingest()
        self.assertEqual(again["inserted"], 0)
        self.assertEqual(self.pipeline.db.stats()["total"], len(self.samples))

        # 只有面试/笔试且置信度达标的才算待通知
        waiting = self.pipeline.db.list_unnotified(self.cfg.notify.categories)
        self.assertEqual(len(waiting), 3, "应为 3 封（腾讯、字节、华为）")

        result = self.pipeline.run_summary()
        self.assertTrue(result["ok"], result.get("note"))
        self.assertEqual(result["notified"], 3)

        # 未完成的会继续每天提醒（续推）：只有回复标记「已完成」后才消失。
        second = self.pipeline.run_summary()
        self.assertEqual(second["notified"], 3, "未完成项第二天应继续推送")
        row = self.pipeline.db.list_pending_jobs(self.cfg.notify.categories)[0]
        self.assertGreaterEqual(row["reminder_count"], 1)

        # 干跑渠道应该留下了汇总文件
        self.assertTrue(list(self.outbox.glob("*-summary.txt")))

    def test_pending_review_not_marked_notified(self):
        self._ingest()
        pending = self.pipeline.db.list_pending_review()
        self.assertEqual(len(pending), 1)
        self.pipeline.run_summary()
        still_pending = self.pipeline.db.list_pending_review()
        self.assertEqual(len(still_pending), 1, "待确认的邮件不应被标记为已通知")
        self.assertIsNone(still_pending[0]["notified_at"])

    def test_ignored_mail_never_notified(self):
        self._ingest()
        rows = self.pipeline.db.list_unnotified(["interview", "written_test", "offer", "rejection"])
        categories = sorted(row["category"] for row in rows)
        self.assertEqual(categories, ["interview", "interview", "written_test"])

    def test_summary_content_contains_key_fields(self):
        self._ingest()
        result = self.pipeline.run_summary()
        self.assertTrue(result["ok"])
        content = sorted(self.outbox.glob("*-summary.txt"))[-1].read_text(encoding="utf-8")
        self.assertIn("招聘邮件汇总", content)
        self.assertIn("腾讯", content)
        self.assertIn("字节跳动", content)
        self.assertIn("待确认", content)

    def test_send_failure_keeps_mail_unnotified(self):
        class BrokenNotifier:
            channel = "broken"

            def send(self, *args, **kwargs):
                raise RuntimeError("模拟 SMTP 故障")

        self._ingest()
        self.pipeline.notifier = BrokenNotifier()
        result = self.pipeline.run_summary()
        self.assertFalse(result["ok"])
        # 发送失败绝不能误标记为已通知，否则这批邮件就永远丢了
        self.assertEqual(len(self.pipeline.db.list_unnotified(self.cfg.notify.categories)), 3)

    def test_poll_with_fake_reader_advances_cursor(self):
        class FakeReader:
            def __init__(self, mails):
                self.mails = mails

            def fetch_new(self, last_uid=None, uid_validity=None, last_fetch_at=None):
                if last_uid is None:
                    return 111, list(self.mails), max(m.uid for m in self.mails)
                return 111, [], last_uid

            def close(self):
                pass

        self.pipeline.reader_factory = lambda cfg: FakeReader([mail for _, mail in self.samples])
        self.cfg.imap.password = "dummy"
        self.pipeline.run_poll()

        cursor = self.pipeline.db.get_cursor(self.cfg.imap.account, self.cfg.imap.mailbox)
        self.assertIsNotNone(cursor, "轮询后应写入游标")
        self.assertEqual(cursor["last_uid"], max(m.uid for _, m in self.samples))
        self.assertEqual(self.pipeline.db.stats()["total"], len(self.samples))


if __name__ == "__main__":
    unittest.main()