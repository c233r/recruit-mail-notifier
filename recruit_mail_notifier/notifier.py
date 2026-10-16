# -*- coding: utf-8 -*-
"""通知渠道。

* SmtpNotifier : 主渠道，把每日汇总发到配置的邮箱（带重试）
* FileNotifier : 干跑 / 本地测试用，把邮件写到 logs/outbox/ 而不真的发送
* WebhookNotifier : 预留的第二个渠道，第一版可保持关闭

凭据全部来自环境变量，这里只负责组装和投递。
"""

from __future__ import annotations

import json
import smtplib
import ssl
import time
import urllib.request
from datetime import datetime, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


class NotifyError(RuntimeError):
    """通知发送失败。"""


def _field(row: Any, key: str, default: str = "") -> str:
    """兼容 sqlite3.Row 与 dict 取值。"""
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else str(value)


def _safe_format(template: str, **kwargs: Any) -> str:
    """模板占位符缺失时不崩溃，原样返回。"""
    try:
        return str(template).format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return str(template)


def format_local(iso_value: Optional[str], tz: Any, pattern: str = "%Y-%m-%d %H:%M") -> str:
    if not iso_value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(tz).strftime(pattern)
    except ValueError:
        return str(iso_value)


def event_label(category: str) -> str:
    return {"interview": "面试时间", "written_test": "笔试时间"}.get(category or "", "相关时间")


def format_deadline(
    iso_value: Optional[str],
    tz: Any,
    now: Optional[datetime] = None,
    fallback: str = "—",
) -> str:
    """把截止时间格式化成「2026-03-08 14:30（3 天后）」。

    邮件里没写截止时间时返回 fallback —— 汇总里传「不限时」比一个破折号清楚。
    """
    if not iso_value:
        return fallback
    try:
        moment = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local = moment.astimezone(tz)
    except ValueError:
        return str(iso_value)

    now = now or datetime.now(tz)
    days = (local.date() - now.date()).days
    if days < 0:
        suffix = "已过期 %d 天" % (-days)
    elif days == 0:
        suffix = "今天"
    elif days == 1:
        suffix = "明天"
    else:
        suffix = "%d 天后" % days
    return "%s（%s）" % (local.strftime("%Y-%m-%d %H:%M"), suffix)


def build_summary_content(
    rows: Sequence[Any],
    cfg: Any,
    tz: Any,
    now: Optional[datetime] = None,
    pending_rows: Optional[Sequence[Any]] = None,
) -> Any:
    """组装汇总邮件的 (主题, 纯文本正文, HTML 正文)。"""
    now = now or datetime.now(tz)
    date_text = now.strftime("%Y-%m-%d")
    total = len(rows)
    subject = _safe_format(
        cfg.notify.subject_template, date=date_text, count=total, total=total
    )

    max_items = max(1, int(cfg.notify.max_items_per_summary or 50))
    shown = list(rows[:max_items])
    pending_rows = list(pending_rows or [])

    # ---------------- 纯文本 ----------------
    lines: List[str] = ["招聘邮件汇总（%s）" % date_text, ""]
    if shown:
        lines.append("共 %d 封待处理通知：" % total)
        lines.append("")
        for row in shown:
            category = _field(row, "category")
            label = _field(row, "category_label") or category
            reminder = int(_field(row, "reminder_count", "0") or 0)
            tail = "（第 %d 次提醒）" % (reminder + 1) if reminder > 0 else ""
            # 用数据库里的记录编号做前缀（回复时写它就能精确标记这一条）
            lines.append(
                "#%s 【%s】%s%s"
                % (_field(row, "id"), label, _field(row, "company") or "未识别公司", tail)
            )
            lines.append("   主题：%s" % (_field(row, "subject") or "（无主题）"))
            sender_addr = parseaddr(_field(row, "sender"))[1] or _field(row, "sender")
            if sender_addr:
                lines.append("   发件人：%s" % sender_addr)
            if _field(row, "position"):
                lines.append("   岗位：%s" % _field(row, "position"))
            if _field(row, "location"):
                lines.append("   地点：%s" % _field(row, "location"))
            lines.append(
                "   截止时间：%s"
                % format_deadline(_field(row, "deadline"), tz, now, "不限时")
            )
            lines.append("   通知时间：%s" % format_local(_field(row, "notify_time"), tz))
            event_iso = _field(row, "event_time")
            event_time = (
                format_local(event_iso, tz)
                if event_iso
                else (_field(row, "event_time_raw") or "未提及")
            )
            lines.append("   %s：%s" % (event_label(category), event_time))
            summary = _field(row, "summary")
            if summary:
                lines.append("   摘要：%s" % summary)
            lines.append("   邮件链接：%s" % (_field(row, "mail_link") or "—"))
            lines.append("")
        if total > len(shown):
            lines.append("（另有 %d 封未在本邮件中列出，可在数据库中查看）" % (total - len(shown)))
            lines.append("")
    else:
        lines.append("今天没有新的面试/笔试通知。")
        lines.append("")

    if pending_rows:
        lines.append("—" * 30)
        lines.append("待确认（置信度偏低，需要你人工看一眼，共 %d 封）：" % len(pending_rows))
        for row in pending_rows[:10]:
            lines.append(
                " · #%s [%s %.2f] %s"
                % (
                    _field(row, "id"),
                    _field(row, "category"),
                    float(_field(row, "confidence", "0") or 0),
                    _field(row, "subject") or "（无主题）",
                )
            )
        if len(pending_rows) > 10:
            lines.append(" · …另有 %d 封" % (len(pending_rows) - 10))
        lines.append("")
        lines.append("可用命令 `python main.py pending` 查看待确认清单。")
        lines.append("")

    lines.append("—" * 30)
    lines.append("直接回复本邮件，在正文里写上要标记完成的编号：")
    lines.append("    #12 #15     推荐：一条一条精确标记（同一家公司有多场笔试/面试时用这个）")
    lines.append("    科大讯飞     按公司名标记，会把这家公司【所有】未完成项一起标掉")
    lines.append("    全部完成     一次清空当前所有提醒")
    lines.append("系统下一轮收信时会处理；没标记的会继续提醒你。")
    lines.append("")
    lines.append("本邮件由招聘邮件通知系统自动发送。")
    text_body = "\n".join(lines)

    # ---------------- HTML ----------------
    html_parts: List[str] = [
        '<div style="font-family:-apple-system,Segoe UI,Microsoft YaHei,sans-serif;'
        'font-size:14px;color:#222;line-height:1.6">',
        "<h2 style=\"margin:0 0 12px\">招聘邮件汇总（%s）</h2>" % date_text,
    ]
    if shown:
        html_parts.append("<p>共 <b>%d</b> 封待处理通知：</p>" % total)
        html_parts.append(
            '<table cellspacing="0" cellpadding="8" border="0" '
            'style="border-collapse:collapse;width:100%;font-size:13px">'
            '<tr style="background:#f2f4f7;text-align:left">'
            "<th>编号</th><th>类别</th><th>公司</th><th>主题</th>"
            "<th>截止时间</th><th>面试/笔试时间</th><th>地点</th><th>摘要</th><th>链接</th></tr>"
        )
        for row in shown:
            category = _field(row, "category")
            event_iso = _field(row, "event_time")
            event_time = (
                format_local(event_iso, tz)
                if event_iso
                else (_field(row, "event_time_raw") or "未提及")
            )
            sender_addr = parseaddr(_field(row, "sender"))[1] or _field(row, "sender")
            subject_html = '%s<br><span style="color:#888;font-size:11px">%s</span>' % (
                _escape(_field(row, "subject")),
                _escape(sender_addr),
            )
            html_parts.append(
                '<tr style="border-bottom:1px solid #e5e7eb">'
                "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                % (
                    "#%s" % _field(row, "id"),
                    _escape(_field(row, "category_label") or category),
                    _escape(_field(row, "company") or "未识别"),
                    subject_html,
                    _escape(format_deadline(_field(row, "deadline"), tz, now, "不限时")),
                    _escape(event_time),
                    _escape(_field(row, "location") or "—"),
                    _escape(_field(row, "summary")),
                    _link_html(_field(row, "mail_link")),
                )
            )
        html_parts.append("</table>")
        if total > len(shown):
            html_parts.append("<p>（另有 %d 封未列出）</p>" % (total - len(shown)))
    else:
        html_parts.append("<p>今天没有新的面试/笔试通知。</p>")

    if pending_rows:
        html_parts.append("<hr><h3>待确认（%d 封）</h3><ul>" % len(pending_rows))
        for row in pending_rows[:10]:
            html_parts.append(
                "<li>#%s [%s %.2f] %s</li>"
                % (
                    _field(row, "id"),
                    _escape(_field(row, "category")),
                    float(_field(row, "confidence", "0") or 0),
                    _escape(_field(row, "subject")),
                )
            )
        html_parts.append("</ul>")
    html_parts.append('<p style="color:#888;font-size:12px">本邮件由招聘邮件通知系统自动发送。</p>')
    html_parts.append("</div>")
    html_body = "".join(html_parts)

    return subject, text_body, html_body


def _escape(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _link_html(link: str) -> str:
    if not link:
        return "—"
    if link.startswith("http"):
        safe = _escape(link)
        return '<a href="%s">打开邮件</a>' % safe
    return _escape(link)


class SmtpNotifier:
    channel = "email"

    def __init__(self, smtp_cfg: Any, logger: Any = None) -> None:
        self.cfg = smtp_cfg
        self.logger = logger

    def _build_message(
        self, subject: str, text_body: str, html_body: Optional[str],
        recipients: Sequence[str], cc: Sequence[str],
    ) -> MIMEMultipart:
        message = MIMEMultipart("alternative")
        message["Subject"] = Header(subject, "utf-8")
        message["From"] = formataddr((str(Header(self.cfg.from_name or "", "utf-8")), self.cfg.sender))
        message["To"] = ", ".join(recipients)
        if cc:
            message["Cc"] = ", ".join(cc)
        message["Date"] = formatdate(localtime=True)
        # 显式生成 Message-ID 并入库，用户的回复才能靠 In-Reply-To 被认出来
        if not message.get("Message-ID"):
            message["Message-ID"] = make_msgid()
        message.attach(MIMEText(text_body, "plain", "utf-8"))
        if html_body:
            message.attach(MIMEText(html_body, "html", "utf-8"))
        return message

    def _deliver(self, message: MIMEMultipart, recipients: Sequence[str]) -> None:
        if self.cfg.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                self.cfg.host, self.cfg.port, timeout=30, context=context
            ) as server:
                server.login(self.cfg.username, self.cfg.password)
                server.sendmail(self.cfg.sender, list(recipients), message.as_string())
        else:
            with smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=30) as server:
                server.ehlo()
                try:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                except smtplib.SMTPException:
                    pass
                server.login(self.cfg.username, self.cfg.password)
                server.sendmail(self.cfg.sender, list(recipients), message.as_string())

    def send(
        self, subject: str, text_body: str, html_body: Optional[str] = None,
        recipients: Optional[Sequence[str]] = None, cc: Optional[Sequence[str]] = None,
    ) -> Optional[str]:
        to_list = [r for r in (recipients or []) if r]
        cc_list = [r for r in (cc or []) if r]
        if not to_list:
            raise NotifyError("mail.ini 的 notify.recipients 为空，没有收件人")
        if not self.cfg.is_ready():
            raise NotifyError(
                "SMTP 配置不完整：请检查 mail.ini 的 smtp.host/username 以及 "
                "password_env 指向的环境变量"
            )

        message = self._build_message(subject, text_body, html_body, to_list, cc_list)
        all_recipients = to_list + cc_list
        attempts = max(1, int(self.cfg.max_retries or 1))
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            try:
                self._deliver(message, all_recipients)
                message_id = str(message.get("Message-ID") or "")
                if self.logger:
                    self.logger.info(
                        "汇总邮件已发送给 %s（第 %d 次尝试），Message-ID=%s",
                        ", ".join(all_recipients), attempt, message_id,
                    )
                return message_id
            except Exception as exc:
                last_error = exc
                if self.logger:
                    self.logger.error("发送失败（第 %d/%d 次）：%s", attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(max(1, int(self.cfg.retry_backoff_seconds or 5)))
        raise NotifyError("汇总邮件发送失败：%s" % last_error)


class FileNotifier:
    """干跑渠道：把汇总写到 logs/outbox/，用于本地验证与测试，不会真的发信。"""

    channel = "file"

    def __init__(self, outbox_dir: Any, logger: Any = None) -> None:
        self.outbox_dir = Path(outbox_dir)
        self.logger = logger

    def send(
        self, subject: str, text_body: str, html_body: Optional[str] = None,
        recipients: Optional[Sequence[str]] = None, cc: Optional[Sequence[str]] = None,
    ) -> str:
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = self.outbox_dir / ("%s-summary" % stamp)
        text_path = base.with_suffix(".txt")
        header = "To: %s\nSubject: %s\n\n" % (", ".join(recipients or []), subject)
        text_path.write_text(header + text_body, encoding="utf-8")
        if html_body:
            base.with_suffix(".html").write_text(html_body, encoding="utf-8")
        message_id = make_msgid()
        if self.logger:
            self.logger.info(
                "[dry-run] 汇总未真实发送，已写入：%s（Message-ID=%s）", text_path, message_id
            )
        return message_id


class WebhookNotifier:
    """预留的第二个通知渠道：把汇总以 JSON POST 到配置的地址。"""

    channel = "webhook"

    def __init__(self, url: str, secret: str = "", logger: Any = None, timeout: int = 15) -> None:
        self.url = url
        self.secret = secret
        self.logger = logger
        self.timeout = timeout

    def send(
        self, subject: str, text_body: str, html_body: Optional[str] = None,
        recipients: Optional[Sequence[str]] = None, cc: Optional[Sequence[str]] = None,
    ) -> None:
        if not self.url:
            raise NotifyError("webhook 未配置 url")
        payload = json.dumps(
            {"subject": subject, "text": text_body, "recipients": list(recipients or [])},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.secret:
            headers["X-Recruit-Secret"] = self.secret
        request = urllib.request.Request(self.url, data=payload, method="POST", headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            response.read()
        if self.logger:
            self.logger.info("Webhook 通知已发送：%s", self.url)
        return None


def build_notifier(cfg: Any, logger: Any = None, dry_run: bool = False) -> Any:
    """按配置挑一个通知渠道。dry_run=True 时永远走文件渠道。"""
    if dry_run:
        return FileNotifier(Path(cfg.log_dir) / "outbox", logger)
    return SmtpNotifier(cfg.smtp, logger)