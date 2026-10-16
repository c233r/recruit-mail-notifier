# -*- coding: utf-8 -*-
import unittest

from recruit_mail_notifier.classifier import classify_mail, extract_company, extract_event_time
from recruit_mail_notifier.config import load_config
from tests.sample_emails import build_sample_mails


class ClassifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.cfg.imap.username = "test@example.com"
        cls.cfg.notify.recipients = ["me@example.com"]
        # 强制只用规则，并保留「主题 + 正文」的全文匹配，保证测试离线且可复现
        # （只看主题的新行为由 tests/test_subject_rules.py 覆盖）
        cls.cfg.classifier.strategy = "rules_only"
        cls.cfg.classifier.rule_scope = "subject_body"
        cls.cfg.model.enabled = False
        cls.samples = build_sample_mails(cls.cfg)

    def test_sample_categories_and_status(self):
        for sample, mail in self.samples:
            with self.subTest(sample=sample["name"]):
                result = classify_mail(mail, self.cfg)
                self.assertEqual(
                    result.category, sample["expect_category"],
                    "%s 分类错误，依据：%s" % (sample["name"], result.reasons),
                )
                self.assertEqual(
                    result.status, sample["expect_status"],
                    "%s 状态错误（置信度 %.2f），依据：%s"
                    % (sample["name"], result.confidence, result.reasons),
                )

    def test_company_extraction(self):
        for sample, mail in self.samples:
            expected = sample.get("expect_company_contains")
            if not expected:
                continue
            with self.subTest(sample=sample["name"]):
                result = classify_mail(mail, self.cfg)
                self.assertIn(expected, result.company)

    def test_event_time_extraction(self):
        for sample, mail in self.samples:
            expected = sample.get("expect_event_raw_contains")
            if not expected:
                continue
            with self.subTest(sample=sample["name"]):
                result = classify_mail(mail, self.cfg)
                self.assertIn(expected, result.event_time_raw)
                self.assertTrue(result.event_time, "面试/笔试时间应能归一化为 ISO 时间")

    def test_irrelevant_mail_is_ignored(self):
        spam = dict(self.samples[4][0])
        result = classify_mail(self.samples[4][1], self.cfg)
        self.assertEqual(result.category, "unknown")
        self.assertEqual(result.status, "ignored")
        self.assertEqual(result.confidence, 0.0)

    def test_low_confidence_marked_pending_review(self):
        sample, mail = self.samples[5]
        result = classify_mail(mail, self.cfg)
        self.assertTrue(result.needs_review)
        self.assertEqual(result.status, "pending_review")

    def test_extract_company_helper(self):
        name = extract_company("【某某科技】面试通知", "hr@x.com", "诚邀您参加某某科技有限公司面试", self.cfg.rules)
        self.assertIn("某某科技", name)
        self.assertEqual(extract_company("你好", "hr@x.com", "没有公司信息", self.cfg.rules), "")

    def test_extract_event_time_chinese_and_iso(self):
        import datetime

        tz = self.cfg.tzinfo()
        now = datetime.datetime(2026, 3, 1, 9, 0, tzinfo=tz)
        iso, raw = extract_event_time("笔试时间：2026-03-10 19:00", tz, now)
        self.assertEqual(iso[:16], "2026-03-10T19:00")
        iso2, raw2 = extract_event_time("面试时间：3月8日 下午2点30分", tz, now)
        self.assertIn("3月8日", raw2)
        self.assertEqual(iso2[:16], "2026-03-08T14:30")

    def test_notify_time_uses_mail_date(self):
        sample, mail = self.samples[0]
        result = classify_mail(mail, self.cfg)
        # 邮件 Date 是 +0800 的 09:12，配置时区也是 +08，应保持 09:12
        self.assertIn("09:12", result.notify_time)


if __name__ == "__main__":
    unittest.main()