# -*- coding: utf-8 -*-
"""完成回复的样例，用于离线测试。"""

from email.message import EmailMessage
from pathlib import Path

from recruit_mail_notifier.mail_reader import ImapReader

# 一封真实的汇总邮件正文（回复时会整段被引用回来）
ORIGINAL_SUMMARY_BODY = (
    "招聘邮件汇总（2026-03-05）\n"
    "\n"
    "共 2 封待处理通知：\n"
    "\n"
    "1. 【面试通知】腾讯科技有限公司\n"
    "   主题：腾讯2026春招面试邀请\n"
    "   截止时间：2026-03-08 14:30（3 天后）\n"
    "   通知时间：2026-03-02 09:12\n"
    "   摘要：我们诚挚邀请您参加腾讯科技有限公司的面试。\n"
    "   邮件链接：mid:tencent-interview-001@tencent.com\n"
    "\n"
    "2. 【笔试通知】字节跳动\n"
    "   主题：【字节跳动】在线笔试通知\n"
    "   截止时间：2026-03-10 19:00（5 天后）\n"
    "   邮件链接：mid:bytedance-test-001@bytedance.com\n"
    "\n"
    "——————————————————————————————\n"
    "待确认（置信度偏低，需要你人工看一眼，共 1 封）：\n"
    " · [interview 0.18] 关于后续安排的沟通\n"
    "\n"
    "——————————————————————————————\n"
    "直接回复本邮件，在正文里写上「已经完成」的公司名，\n"
    "系统下一轮收信时会把这些公司从提醒里去掉；没写完的会继续提醒你。\n"
    "写「全部完成」可以一次清空当前所有提醒。\n"
    "\n"
    "本邮件由招聘邮件通知系统自动发送。\n"
)

REPLY_QUOTED = "腾讯\n\n在 2026年3月5日 10:05，招聘邮件助手 <bot@example.com> 写道：\n" + ORIGINAL_SUMMARY_BODY
REPLY_ONLY_QUOTED = "在 2026年3月5日 10:05，招聘邮件助手 <bot@example.com> 写道：\n" + ORIGINAL_SUMMARY_BODY
REPLY_MULTI = "腾讯\n字节跳动\n华为\n"
REPLY_ALL = "全部完成\n"

# 用户在 QQ 邮箱里真实回复的样子（含签名）
QQ_REPLY = (
    "广东电信 已完成\n"
    "科大讯飞 已完成\n"
    "中国移动通信集团四川有限公司 已完成\n"
    "\n"
    "No&nbsp;name\n"
    "your_account@qq.com\n"
)

# 系统自己发出去的汇总邮件（会被下一轮拉取收回来，必须能识别出来）
OWN_SUMMARY_SUBJECT = "【招聘汇总】2026-09-30 共 39 封待处理"

REPLY_GMAIL_STYLE = "字节跳动\n\nOn Thu, Mar 5, 2026 at 10:05 AM bot <bot@example.com> wrote:\n" + ORIGINAL_SUMMARY_BODY


def build_reply_mail(
    cfg,
    body,
    subject="Re: 【招聘汇总】2026-03-05 共 2 封待处理",
    sender="me@example.com",
    message_id="<reply-001@example.com>",
    uid=5000,
    quoted=False,
    in_reply_to="<summary-abc@example.com>",
):
    """把一段回复正文构造成 RawMail，走真实的 MIME 解析路径。"""
    reader = ImapReader(cfg.imap, logger=None, tz=cfg.tzinfo())
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "bot@example.com"
    message["Date"] = "Thu, 05 Mar 2026 20:00:00 +0800"
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    message.set_content(body)
    return reader._parse(uid, message.as_bytes(), 111)


def write_eml_files(directory):
    # 把几种典型的完成回复导出成 .eml，方便用 main.py classify-eml 手动试
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = [
        ("reply_tencent_done", REPLY_QUOTED),
        ("reply_multi_done", REPLY_MULTI),
        ("reply_all_done", REPLY_ALL),
        ("reply_only_quoted", REPLY_ONLY_QUOTED),
    ]
    written = []
    for name, body in cases:
        message = EmailMessage()
        message["Subject"] = "Re: 【招聘汇总】2026-03-05 共 2 封待处理"
        message["From"] = "me@example.com"
        message["To"] = "bot@example.com"
        message["Date"] = "Thu, 05 Mar 2026 20:00:00 +0800"
        message["Message-ID"] = "<%s@example.com>" % name
        message["In-Reply-To"] = "<summary-abc@example.com>"
        message.set_content(body)
        path = out_dir / ("%s.eml" % name)
        path.write_bytes(message.as_bytes())
        written.append(path)
    return written
