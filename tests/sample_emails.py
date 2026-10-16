# -*- coding: utf-8 -*-
"""本地测试用的样例邮件。

全部用 email.message.EmailMessage 在内存里构造，不依赖网络，
也不会读写你的真实邮箱。每封样例都带上了期望的分类结果，供断言使用。
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Tuple

from recruit_mail_notifier.mail_reader import ImapReader

SAMPLES: List[Dict[str, Any]] = [
    {
        "name": "interview_tencent",
        "subject": "腾讯2026春招面试邀请",
        "sender": "腾讯招聘 <campus@tencent.com>",
        "to": "me@example.com",
        "date": "Mon, 02 Mar 2026 09:12:00 +0800",
        "message_id": "<tencent-interview-001@tencent.com>",
        "body": (
            "同学您好：\n\n"
            "我们诚挚邀请您参加腾讯科技有限公司 2026 届校园招聘的面试。\n\n"
            "面试时间：2026年3月8日 14:30\n"
            "面试地点：深圳市南山区腾讯滨海大厦 32 楼\n"
            "面试形式：视频面试（腾讯会议 https://meeting.tencent.com/dm/abc123）\n"
            "面试官：张老师\n\n"
            "请在面试开始前 10 分钟进入会议室。如时间冲突请提前邮件告知。\n"
        ),
        "expect_category": "interview",
        "expect_status": "new",
        "expect_company_contains": "腾讯",
        "expect_event_raw_contains": "3月8日",
    },
    {
        "name": "written_test_bytedance",
        "subject": "【字节跳动】在线笔试通知",
        "sender": "字节跳动校园招聘 <campus@bytedance.com>",
        "to": "me@example.com",
        "date": "Mon, 02 Mar 2026 10:30:00 +0800",
        "message_id": "<bytedance-test-001@bytedance.com>",
        "body": (
            "您好，您投递的岗位已进入笔试环节。\n\n"
            "笔试时间：2026-03-10 19:00\n"
            "笔试链接：https://exam.nowcoder.com/test/12345\n"
            "在线笔试时长 90 分钟，请提前准备好摄像头与稳定的网络。\n"
        ),
        "expect_category": "written_test",
        "expect_status": "new",
        "expect_event_raw_contains": "2026-03-10",
    },
    {
        "name": "offer_alibaba",
        "subject": "录用通知 - 阿里巴巴",
        "sender": "阿里巴巴招聘 <talent@alibaba-inc.com>",
        "to": "me@example.com",
        "date": "Mon, 02 Mar 2026 11:00:00 +0800",
        "message_id": "<alibaba-offer-001@alibaba-inc.com>",
        "body": (
            "恭喜您通过全部面试，我们决定录用您。\n"
            "该录用通知来自阿里巴巴集团，入职时间与薪资细节将另行沟通。\n"
        ),
        "expect_category": "offer",
        "expect_status": "ignored",
    },
    {
        "name": "rejection_meituan",
        "subject": "感谢您参加美团面试",
        "sender": "美团招聘 <hr@meituan.com>",
        "to": "me@example.com",
        "date": "Mon, 02 Mar 2026 12:00:00 +0800",
        "message_id": "<meituan-reject-001@meituan.com>",
        "body": "很遗憾地通知您，本次面试未通过。祝您前程似锦。\n",
        "expect_category": "rejection",
        "expect_status": "ignored",
    },
    {
        "name": "spam_promotion",
        "subject": "限时优惠，全场五折起",
        "sender": "促销中心 <promo@shop.example.com>",
        "to": "me@example.com",
        "date": "Mon, 02 Mar 2026 13:00:00 +0800",
        "message_id": "<spam-001@shop.example.com>",
        "body": "本周大促，满 199 减 100。退订请回复 TD。\n",
        "expect_category": "unknown",
        "expect_status": "ignored",
    },
    {
        "name": "vague_pending_review",
        "subject": "关于后续安排的沟通",
        "sender": "某公司HR <hr@unknown-corp.com>",
        "to": "me@example.com",
        "date": "Mon, 02 Mar 2026 14:00:00 +0800",
        "message_id": "<vague-001@unknown-corp.com>",
        "body": "您好，想和您约个时间聊聊后续的面试相关事宜，具体时间待定。\n",
        "expect_category": "interview",
        "expect_status": "pending_review",
    },
    {
        "name": "interview_huawei_html_no_message_id",
        "subject": "【华为】面试通知",
        "sender": "华为招聘 <hr@huawei.com>",
        "to": "me@example.com",
        "date": "Mon, 02 Mar 2026 15:00:00 +0800",
        "message_id": None,
        "html": True,
        "body": (
            "<html><body>"
            "<p>您好，诚邀您参加华为技术有限公司的面试。</p>"
            "<p>面试时间：3月12日 上午10:00</p>"
            "<p>面试地点：线上（视频面试）</p>"
            "</body></html>"
        ),
        "expect_category": "interview",
        "expect_status": "new",
        "expect_company_contains": "华为",
        "expect_event_raw_contains": "3月12日",
    },
]


def build_message(sample: Dict[str, Any]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = sample["subject"]
    message["From"] = sample["sender"]
    message["To"] = sample.get("to", "me@example.com")
    if sample.get("date"):
        message["Date"] = sample["date"]
    if sample.get("message_id"):
        message["Message-ID"] = sample["message_id"]
    subtype = "html" if sample.get("html") else "plain"
    message.set_content(sample["body"], subtype=subtype)
    return message


def build_sample_mails(cfg: Any, start_uid: int = 1000) -> List[Tuple[Dict[str, Any], Any]]:
    """把样例邮件解析成 RawMail。复用 ImapReader 的解析逻辑（不联网）。"""
    reader = ImapReader(cfg.imap, logger=None, tz=cfg.tzinfo())
    result = []
    for index, sample in enumerate(SAMPLES):
        message = build_message(sample)
        mail = reader._parse(start_uid + index, message.as_bytes(), 111)
        result.append((sample, mail))
    return result


def write_eml_files(directory: Any) -> List[Path]:
    """把样例导出成 .eml 文件，便于用 main.py classify-eml 手动试。"""
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for sample in SAMPLES:
        path = out_dir / ("%s.eml" % sample["name"])
        path.write_bytes(build_message(sample).as_bytes())
        written.append(path)
    return written