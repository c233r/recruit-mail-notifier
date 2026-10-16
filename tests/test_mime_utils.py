# -*- coding: utf-8 -*-
import unittest
from email.message import EmailMessage

from recruit_mail_notifier.mime_utils import (
    build_summary,
    decode_mime_header,
    extract_body_text,
    fallback_message_id,
    html_to_text,
    parse_mail_date,
)


class MimeUtilsTest(unittest.TestCase):
    def test_decode_mime_header(self):
        # =?utf-8?B?5L2g5aW9?= 就是「你好」
        self.assertEqual(decode_mime_header("=?utf-8?B?5L2g5aW9?="), "你好")
        self.assertEqual(decode_mime_header(""), "")
        self.assertEqual(decode_mime_header("普通主题"), "普通主题")

    def test_html_to_text(self):
        text = html_to_text("<html><body><p>面试时间</p><br>14:30</body></html>")
        self.assertIn("面试时间", text)
        self.assertIn("14:30", text)
        self.assertNotIn("<", text)

    def test_extract_body_text_prefers_plain(self):
        message = EmailMessage()
        message["Subject"] = "test"
        message.set_content("纯文本正文")
        message.add_alternative("<p>HTML 正文</p>", subtype="html")
        self.assertIn("纯文本正文", extract_body_text(message))

    def test_extract_body_text_falls_back_to_html(self):
        message = EmailMessage()
        message.set_content("<p>只有HTML的面试通知</p>", subtype="html")
        self.assertIn("只有HTML的面试通知", extract_body_text(message))

    def test_build_summary_prefers_time_line(self):
        body = "恭喜您\n\n随便一句无关的话\n\n面试时间：2026年3月8日 14:30\n"
        summary = build_summary(body, max_chars=120)
        self.assertIn("3月8日", summary)

    def test_build_summary_drops_noise_lines(self):
        body = "退订请回复TD\n面试时间：3月8日 14:30\n"
        summary = build_summary(body, max_chars=120, drop_patterns=["退订"])
        self.assertNotIn("退订", summary)
        self.assertIn("3月8日", summary)

    def test_parse_mail_date(self):
        moment = parse_mail_date("Mon, 02 Mar 2026 09:12:00 +0800")
        self.assertIsNotNone(moment)
        self.assertEqual(moment.year, 2026)
        self.assertIsNone(parse_mail_date("not a date"))

    def test_fallback_message_id_is_stable(self):
        first = fallback_message_id("a@b.com", 111, 1000, "主题", "发件人")
        second = fallback_message_id("a@b.com", 111, 1000, "主题", "发件人")
        third = fallback_message_id("a@b.com", 111, 1001, "主题", "发件人")
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)


if __name__ == "__main__":
    unittest.main()