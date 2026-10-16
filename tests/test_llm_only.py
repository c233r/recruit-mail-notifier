# -*- coding: utf-8 -*-
"""strategy = llm_only：完全交给模型，模型按固定 JSON 返回字段。"""

import json
import logging
import tempfile
import unittest
from pathlib import Path

from recruit_mail_notifier.classifier import classify_mail
from recruit_mail_notifier.config import load_config
from recruit_mail_notifier.db import (
    CATEGORY_UNKNOWN,
    STATUS_IGNORED,
    STATUS_NEW,
    STATUS_PENDING_REVIEW,
)
from recruit_mail_notifier.llm_client import LlmClient, LlmResult
from recruit_mail_notifier.mail_reader import RawMail
from recruit_mail_notifier.notifier import FileNotifier
from recruit_mail_notifier.pipeline import Pipeline

FULL_JSON = {
    "category": "interview",
    "is_job_notification": True,
    "company": "腾讯科技有限公司",
    "position": "数据引擎研发工程师",
    "event_time_text": "2026年3月8日 14:30",
    "deadline_text": "请在3月5日前回复确认",
    "location": "深圳市南山区腾讯滨海大厦 32 楼",
    "summary": "腾讯邀请你 3月8日 14:30 参加视频面试，地点深圳滨海大厦。",
    "confidence": 0.93,
    "reason": "主题和正文都明确是面试邀约，并给出时间地点",
}


def _mail(subject="【某公司】面试邀约", body="", received_at="2026-03-02T01:12:00+00:00"):
    return RawMail(
        uid=1,
        uid_validity=1,
        message_id="<x@y>",
        subject=subject,
        sender="hr@example.com",
        recipients="me@example.com",
        date_header="",
        received_at=received_at,
        body_text=body,
        mail_link="mid:x@y",
        mailbox="INBOX",
        account="me@example.com",
    )


class FakeLlm:
    """假模型：直接返回预设字段，不发网络请求。"""

    def __init__(self, payload=None, ok=True, error=""):
        self.payload = dict(FULL_JSON if payload is None else payload)
        self.ok = ok
        self.error = error

    def classify(self, subject, sender, body, received_local="", tz_name=""):
        if not self.ok:
            return LlmResult(ok=False, error=self.error or "模拟失败")
        return LlmResult(ok=True, **self.payload)


class LlmOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)

        self.cfg = load_config()
        self.cfg.imap.username = "test@example.com"
        self.cfg.notify.recipients = ["me@example.com"]
        self.cfg.classifier.strategy = "llm_only"
        self.cfg.model.enabled = True
        self.cfg.model.base_url = "https://example.invalid/v1"
        self.cfg.model.model = "fake-model"
        self.cfg.model.api_key = "test-key"
        self.cfg.db_path = tmp / "test.db"
        self.cfg.log_dir = tmp / "logs"

        self.pipeline = Pipeline(
            config=self.cfg,
            logger=logging.getLogger("test"),
            notifier=FileNotifier(tmp / "outbox"),
        )
        self.pipeline.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    # ------------------------------------------------------------ 字段填充
    def test_all_fields_filled_from_llm(self):
        result = classify_mail(_mail(), self.cfg, llm_client=FakeLlm())
        self.assertEqual(result.category, "interview")
        self.assertEqual(result.status, STATUS_NEW)
        self.assertEqual(result.source, "llm")
        self.assertEqual(result.company, "腾讯科技有限公司")
        self.assertEqual(result.position, "数据引擎研发工程师")
        self.assertEqual(result.location, "深圳市南山区腾讯滨海大厦 32 楼")
        self.assertEqual(result.summary, FULL_JSON["summary"])
        self.assertAlmostEqual(result.confidence, 0.93)
        # 模型只摘原文，归一化成 ISO 由正则负责
        self.assertIn("2026-03-08T14:30", result.event_time)
        self.assertIn("2026-03-05", result.deadline)

    def test_rules_do_not_interfere(self):
        # 主题明明写着「笔试」，模型说是面试 —— llm_only 下以模型为准
        mail = _mail(subject="【某公司】在线笔试通知", body="笔试时间：2026年3月10日 19:00")
        result = classify_mail(mail, self.cfg, llm_client=FakeLlm())
        self.assertEqual(result.category, "interview")
        self.assertEqual(result.source, "llm")

    def test_missing_llm_fields_fall_back_to_regex(self):
        payload = dict(FULL_JSON)
        payload.update({"event_time_text": "", "deadline_text": "", "summary": "", "position": "", "location": ""})
        mail = _mail(body="面试时间：2026年3月9日 10:00\n面试地点：线上")
        result = classify_mail(mail, self.cfg, llm_client=FakeLlm(payload))
        self.assertIn("2026-03-09", result.event_time)
        self.assertTrue(result.summary)
        self.assertEqual(result.position, "")
        self.assertEqual(result.location, "")

    # ------------------------------------------------------------ 状态
    def test_unknown_is_ignored(self):
        payload = {"category": "unknown", "is_job_notification": False,
                   "confidence": 0.95, "reason": "验证码邮件"}
        result = classify_mail(_mail(subject="【某网站】验证码"), self.cfg,
                               llm_client=FakeLlm(payload))
        self.assertEqual(result.status, STATUS_IGNORED)
        self.assertEqual(result.category, CATEGORY_UNKNOWN)

    def test_other_category_is_not_pushed(self):
        payload = {"category": "other", "is_job_notification": True,
                   "confidence": 0.9, "company": "某公司", "reason": "投递成功回执"}
        result = classify_mail(_mail(subject="投递成功"), self.cfg, llm_client=FakeLlm(payload))
        self.assertEqual(result.status, STATUS_IGNORED)

    def test_low_confidence_is_pending_review(self):
        payload = {"category": "interview", "is_job_notification": True,
                   "confidence": 0.40, "reason": "不太确定"}
        result = classify_mail(_mail(), self.cfg, llm_client=FakeLlm(payload))
        self.assertEqual(result.status, STATUS_PENDING_REVIEW)

    # ------------------------------------------------------------ 降级
    def test_model_failure_falls_back_to_pending_review(self):
        result = classify_mail(_mail(), self.cfg, llm_client=FakeLlm(ok=False))
        self.assertEqual(result.status, STATUS_PENDING_REVIEW)
        self.assertIn("模型调用失败", " ".join(result.reasons))

    def test_model_not_ready_falls_back_to_rules(self):
        self.cfg.model.enabled = False
        result = classify_mail(_mail(), self.cfg, llm_client=FakeLlm())
        self.assertEqual(result.source, "rules")
        self.assertEqual(result.status, STATUS_PENDING_REVIEW)

    # ------------------------------------------------------------ 入库
    def test_pipeline_stores_llm_fields(self):
        self.pipeline.store_mail(_mail(), llm_client=FakeLlm(), budget=None)
        row = self.pipeline.db.get_message_by_message_id("<x@y>")
        self.assertEqual(row["company"], "腾讯科技有限公司")
        self.assertEqual(row["position"], "数据引擎研发工程师")
        self.assertEqual(row["location"], "深圳市南山区腾讯滨海大厦 32 楼")
        self.assertEqual(row["source"], "llm")
        self.assertIn("2026-03-08", row["event_time"])

    # ------------------------------------------------------------ 解析
    def test_llm_client_parses_full_json(self):
        client = LlmClient(
            self.cfg.model, self.cfg.classifier,
            ["interview", "written_test", "offer", "rejection", "other", "unknown"],
        )
        parsed = client._extract_json(json.dumps(FULL_JSON, ensure_ascii=False))
        self.assertEqual(parsed["category"], "interview")
        self.assertEqual(parsed["position"], "数据引擎研发工程师")
        self.assertEqual(parsed["deadline_text"], "请在3月5日前回复确认")

    def test_llm_client_accepts_json_code_block(self):
        client = LlmClient(self.cfg.model, self.cfg.classifier, ["interview", "unknown"])
        content = "```json\n" + json.dumps(FULL_JSON, ensure_ascii=False) + "\n```"
        parsed = client._extract_json(content)
        self.assertEqual(parsed["company"], "腾讯科技有限公司")


if __name__ == "__main__":
    unittest.main()
