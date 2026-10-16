# -*- coding: utf-8 -*-
"""「只看主题的规则快判 + 模型兜底」行为测试。

用例全部取自真实邮箱里踩过的坑：宣讲会邀请、投递成功回执、防诈骗提醒、
满意度问卷，以及历史邮件年份被算错、模板时间戳被当成截止时间。
"""

import unittest

from recruit_mail_notifier.classifier import classify_mail, match_subject_rule
from recruit_mail_notifier.config import load_config
from recruit_mail_notifier.db import (
    CATEGORY_UNKNOWN,
    STATUS_IGNORED,
    STATUS_NEW,
    STATUS_PENDING_REVIEW,
)
from recruit_mail_notifier.llm_client import LlmResult
from recruit_mail_notifier.mail_reader import RawMail


def _mail(subject, body="", received_at="2026-09-23T01:00:00+00:00"):
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
    """假模型：不发网络请求，直接返回预设结果。"""

    def __init__(self, category="interview", confidence=0.9, company="模型给的公司", ok=True):
        self.category = category
        self.confidence = confidence
        self.company = company
        self.ok = ok

    def classify(self, subject, sender, body, received_local="", tz_name=""):
        return LlmResult(
            ok=self.ok,
            category=self.category,
            confidence=self.confidence,
            company=self.company,
            reason="测试用假模型",
        )


class SubjectRuleTest(unittest.TestCase):
    def setUp(self):
        # 每个用例重新读一次配置，避免模型开关在用例之间串味
        self.cfg = load_config()
        self.cfg.imap.username = "test@example.com"
        self.cfg.notify.recipients = ["me@example.com"]
        # 这个文件测的是「规则参与」的那条路径；llm_only 由 test_llm_only 覆盖
        self.cfg.classifier.strategy = "rules_then_llm"
        self.cfg.classifier.rule_scope = "subject"
        self.cfg.model.enabled = False

    def _enable_fake_model(self):
        """打开模型开关，让 classify_mail 认为模型可用（实际传的是 FakeLlm，不发网络请求）。"""
        self.cfg.model.enabled = True
        self.cfg.model.base_url = "https://example.invalid/v1"
        self.cfg.model.model = "fake-model"
        self.cfg.model.api_key = "test-key"

    # ------------------------------------------------------------ 主题匹配
    def test_match_subject_rule_basics(self):
        self.assertEqual(
            match_subject_rule("小米集团测评邀请", self.cfg.rules), ("written_test", "测评")
        )
        self.assertEqual(
            match_subject_rule("来自吉利控股集团的AI面试邀请", self.cfg.rules),
            ("interview", "面试"),
        )
        self.assertEqual(
            match_subject_rule("【新凯来】诚邀您参加新凯来在线笔试，谢谢！", self.cfg.rules),
            ("written_test", "笔试"),
        )

    def test_match_subject_rule_misses(self):
        self.assertIsNone(match_subject_rule("", self.cfg.rules))
        self.assertIsNone(
            match_subject_rule("荣耀终端股份有限公司招聘官网注册邮箱激活码：627455", self.cfg.rules)
        )
        self.assertIsNone(match_subject_rule("开启中国平安之旅", self.cfg.rules))

    def test_negative_subject_is_not_fast_pathed(self):
        # 满意度问卷虽然带「面试」，但用了排除词，不做快判
        self.assertIsNone(
            match_subject_rule("智元创新（上海）科技股份有限公司面试满意度问卷", self.cfg.rules)
        )
        self.assertIsNone(match_subject_rule("感谢您参加美团面试", self.cfg.rules))

    # ------------------------------------------------------------ 快判直接推送
    def test_subject_hit_sends_directly_without_model(self):
        # 正文里没有任何关键词也照样推送，因为主题命中了
        result = classify_mail(_mail("小米集团测评邀请", "请您点击链接进入测评平台"), self.cfg)
        self.assertEqual(result.category, "written_test")
        self.assertEqual(result.status, STATUS_NEW)
        self.assertEqual(result.source, "rules")
        self.assertFalse(result.needs_review)

    # ------------------------------------------------------------ 正文命中不再误报
    def test_body_only_keyword_is_not_pushed(self):
        # 这正是之前的误报来源：主题只是「诚邀您投递」，正文里写着面试/笔试
        mail = _mail(
            "汕尾移动2027年秋季校园招聘诚邀您投递",
            "笔试及测评 点击直达链接： 笔试、面试通知将主要通过短信或邮件方式发送。",
        )
        result = classify_mail(mail, self.cfg)
        self.assertNotEqual(result.status, STATUS_NEW, "只命中正文不能直接推送")

    def test_anti_fraud_mail_is_not_pushed(self):
        result = classify_mail(
            _mail("QQ邮箱安全提醒：警惕招聘欺诈等7类诈骗！", "遇到要求转账的面试请警惕。"), self.cfg
        )
        self.assertNotEqual(result.status, STATUS_NEW)

    # ------------------------------------------------------------ 模型未启用时的兜底
    def test_no_model_marks_pending_review(self):
        result = classify_mail(
            _mail("中国联通重庆市分公司2027校园招聘宣讲", "宣讲结束后同步安排现场面试。"), self.cfg
        )
        self.assertEqual(result.status, STATUS_PENDING_REVIEW)
        self.assertTrue(result.needs_review)

    def test_no_signal_is_ignored(self):
        result = classify_mail(_mail("您 Steam 愿望单上的游戏正在特卖！", "限时优惠，退订请回复TD。"), self.cfg)
        self.assertEqual(result.status, STATUS_IGNORED)
        self.assertEqual(result.category, CATEGORY_UNKNOWN)

    # ------------------------------------------------------------ 模型兜底
    def test_model_decides_when_subject_misses(self):
        self._enable_fake_model()
        mail = _mail("【某公司】后续安排通知", "诚邀您参加线上面试。")
        result = classify_mail(mail, self.cfg, llm_client=FakeLlm(category="interview", confidence=0.88))
        self.assertEqual(result.category, "interview")
        self.assertEqual(result.source, "llm")
        self.assertEqual(result.status, STATUS_NEW)
        # 规则抽的公司名是「某公司」，应被模型给出的覆盖
        self.assertEqual(result.company, "模型给的公司")

    def test_model_says_unknown_is_ignored(self):
        self._enable_fake_model()
        # 主题没命中关键词，但正文有噪声词；模型判定无关就应该忽略
        mail = _mail("【某公司】后续安排通知", "诚邀您参加线上面试。")
        result = classify_mail(mail, self.cfg, llm_client=FakeLlm(category="unknown", confidence=0.2))
        self.assertEqual(result.status, STATUS_IGNORED)
        self.assertEqual(result.category, CATEGORY_UNKNOWN)

    def test_model_failure_falls_back_to_pending_review(self):
        self._enable_fake_model()
        mail = _mail("【某公司】后续安排通知", "诚邀您参加线上面试。")
        result = classify_mail(mail, self.cfg, llm_client=FakeLlm(ok=False))
        self.assertEqual(result.status, STATUS_PENDING_REVIEW)

    # ------------------------------------------------------------ 日期修正
    def test_historical_mail_keeps_correct_year(self):
        # 2026-09-23 收到、写着「09月26日」的邮件，年份应该是 2026 而不是 2027
        mail = _mail(
            "中国电信各分公司邀请您参加线上笔试",
            "计算机类：北京时间09月26日 09:00-11:00（周六），笔试地址：https://example.com",
        )
        result = classify_mail(mail, self.cfg)
        self.assertIn("2026-09-26", result.deadline)

    def test_absurd_template_timestamp_is_rejected(self):
        mail = _mail(
            "中国移动广西公司2027届秋季校园招聘正式启动",
            "报名截止时间以官网为准。模板时间戳 2000-04-20 00:00 仅供参考。",
        )
        result = classify_mail(mail, self.cfg)
        self.assertNotIn("2000-04-20", result.deadline or "")
        self.assertNotIn("2000-04-20", result.event_time or "")

    def test_month_day_rolls_forward_only_within_a_year(self):
        # 收到时间在 9 月底、通知写 10 月 24 日 -> 应是今年（日期还没到）
        mail = _mail("中国移动广西公司2027届秋季校园招聘正式启动", "笔试时间为10月24日；")
        result = classify_mail(mail, self.cfg)
        self.assertIn("2026-10-24", result.event_time or result.deadline)


if __name__ == "__main__":
    unittest.main()
