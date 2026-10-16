# -*- coding: utf-8 -*-
import unittest

from recruit_mail_notifier import replies
from recruit_mail_notifier.config import load_config
from tests import sample_replies as sr


class RepliesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.rules = cls.cfg.rules

    def test_classify_incoming_three_way(self):
        # 系统自己发出的汇总：主题像汇总，但没有回复前缀、没有 In-Reply-To
        self.assertEqual(
            replies.classify_incoming(sr.OWN_SUMMARY_SUBJECT, "", "", sr.ORIGINAL_SUMMARY_BODY),
            replies.OWN_SUMMARY,
        )
        # 用户回复：带「回复：」前缀
        self.assertEqual(
            replies.classify_incoming(
                "回复：" + sr.OWN_SUMMARY_SUBJECT, "<x@qq.com>", "<x@qq.com>", "腾讯 已完成"
            ),
            replies.COMPLETION_REPLY,
        )
        # 有 In-Reply-To 但不是汇总主题 -> 其他
        self.assertEqual(
            replies.classify_incoming("关于下周的安排", "<x@y>", "", "好的"), replies.OTHER
        )
        self.assertEqual(replies.classify_incoming("面试邀请：腾讯", "", "", ""), replies.OTHER)

    def test_own_summary_never_looks_like_a_reply(self):
        self.assertFalse(replies.looks_like_summary_reply(sr.OWN_SUMMARY_SUBJECT, "", "", []))

    def test_own_summary_body_yields_no_mentions(self):
        # 自己的汇总正文里全是「岗位：」「地点：」，不能被当成用户输入
        parsed = replies.parse_completion_body(sr.ORIGINAL_SUMMARY_BODY, self.rules)
        self.assertEqual(parsed.mentions, [])

    def test_real_qq_reply_parses_and_filters_signature(self):
        parsed = replies.parse_completion_body(sr.QQ_REPLY, self.rules)
        self.assertEqual(
            parsed.mentions,
            ["广东电信", "科大讯飞", "中国移动通信集团四川有限公司"],
        )
        self.assertTrue(parsed.has_completion_word)

    def test_ids_are_extracted(self):
        parsed = replies.parse_completion_body("#12 #15 已完成\n编号 18\nid:20", self.rules)
        self.assertEqual(parsed.ids, [12, 15, 18, 20])

    def test_ids_win_over_company_names(self):
        pending = [
            {"id": 12, "company": "科大讯飞", "subject": "科大讯飞测评通知"},
            {"id": 15, "company": "科大讯飞", "subject": "科大讯飞笔试通知"},
            {"id": 18, "company": "科大讯飞", "subject": "科大讯飞面试通知"},
        ]
        parsed = replies.parse_completion_body("#12 已完成\n科大讯飞", self.rules)
        matched = replies.match_pending_companies(parsed, pending, self.rules)
        self.assertEqual(
            [row["id"] for row in matched], [12],
            "写了编号时只按编号匹配，不能被公司名连带标掉其他的",
        )

    def test_company_matching_still_used_without_ids(self):
        pending = [
            {"id": 12, "company": "科大讯飞", "subject": "科大讯飞测评通知"},
            {"id": 15, "company": "腾讯科技有限公司", "subject": "腾讯面试邀请"},
        ]
        parsed = replies.parse_completion_body("腾讯 已完成", self.rules)
        matched = replies.match_pending_companies(parsed, pending, self.rules)
        self.assertEqual([row["id"] for row in matched], [15])

    def test_hash_number_is_not_treated_as_a_company(self):
        parsed = replies.parse_completion_body("#12", self.rules)
        self.assertEqual(parsed.ids, [12])
        self.assertNotIn("12", parsed.mentions)

    def test_real_qq_reply_matches_pending_rows(self):
        pending = [
            {"id": 1, "company": "广东电信", "subject": "广东电信2027校园招聘线下面试"},
            {"id": 2, "company": "科大讯飞", "subject": "【讯飞招聘】测评通知"},
            {"id": 3, "company": "中国移动通信集团四川有限公司", "subject": "四川移动校招面试"},
            {"id": 4, "company": "腾讯科技有限公司", "subject": "腾讯面试邀请"},
        ]
        parsed = replies.parse_completion_body(sr.QQ_REPLY, self.rules)
        matched = replies.match_pending_companies(parsed, pending, self.rules)
        self.assertEqual([row["id"] for row in matched], [1, 2, 3])

    def test_looks_like_summary_reply(self):
        self.assertTrue(replies.looks_like_summary_reply("Re: 【招聘汇总】2026-03-05 共 2 封待处理"))
        self.assertTrue(replies.looks_like_summary_reply("回复：【招聘汇总】2026-03-05 共 2 封待处理"))
        self.assertTrue(replies.looks_like_summary_reply(
            "随便什么主题", "<summary-abc@example.com>", "", ["<summary-abc@example.com>"]))
        self.assertFalse(replies.looks_like_summary_reply("面试邀请：腾讯"))
        self.assertFalse(replies.looks_like_summary_reply("Re: 关于下周的安排"))

    def test_strip_quoted_keeps_only_new_text(self):
        self.assertEqual(replies.strip_quoted(sr.REPLY_QUOTED).strip(), "腾讯")

    def test_gmail_style_quote_is_stripped(self):
        self.assertEqual(replies.strip_quoted(sr.REPLY_GMAIL_STYLE).strip(), "字节跳动")

    def test_quoted_only_reply_yields_nothing(self):
        # 这是最关键的一条：回复里只有引用原文时，绝不能解析出公司名
        parsed = replies.parse_completion_body(sr.REPLY_ONLY_QUOTED, self.rules)
        self.assertEqual(parsed.mentions, [])
        self.assertFalse(parsed.mark_all)
        self.assertTrue(parsed.is_empty)

    def test_parse_mentions_and_all(self):
        parsed = replies.parse_completion_body(sr.REPLY_QUOTED, self.rules)
        self.assertEqual(parsed.mentions, ["腾讯"])
        self.assertFalse(parsed.mark_all)

        multi = replies.parse_completion_body(sr.REPLY_MULTI, self.rules)
        self.assertEqual(multi.mentions, ["腾讯", "字节跳动", "华为"])

        every = replies.parse_completion_body(sr.REPLY_ALL, self.rules)
        self.assertTrue(every.mark_all)
        self.assertTrue(every.has_completion_word)

    def test_completion_word_detected(self):
        self.assertTrue(replies.parse_completion_body("腾讯已经面完了", self.rules).has_completion_word)
        self.assertFalse(replies.parse_completion_body("腾讯", self.rules).has_completion_word)

    def test_company_core(self):
        self.assertEqual(replies.company_core("腾讯科技有限公司", self.rules), "腾讯")
        self.assertEqual(replies.company_core("字节跳动", self.rules), "字节跳动")

    def test_match_pending_companies_uses_core_name(self):
        pending = [
            {"id": 1, "company": "腾讯科技有限公司", "subject": "腾讯2026春招面试邀请"},
            {"id": 2, "company": "字节跳动", "subject": "【字节跳动】在线笔试通知"},
        ]
        parsed = replies.parse_completion_body("腾讯\n", self.rules)
        matched = replies.match_pending_companies(parsed, pending, self.rules)
        self.assertEqual([row["id"] for row in matched], [1])

    def test_quoted_reply_matches_nothing(self):
        pending = [
            {"id": 1, "company": "腾讯科技有限公司", "subject": "腾讯2026春招面试邀请"},
            {"id": 2, "company": "字节跳动", "subject": "【字节跳动】在线笔试通知"},
        ]
        parsed = replies.parse_completion_body(sr.REPLY_ONLY_QUOTED, self.rules)
        self.assertEqual(replies.match_pending_companies(parsed, pending, self.rules), [])


if __name__ == "__main__":
    unittest.main()
