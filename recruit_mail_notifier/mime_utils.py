# -*- coding: utf-8 -*-
"""邮件 MIME 解码与文本处理：主题解码、正文提取、摘要生成。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Iterable, List, Optional, Sequence

_WS_RE = re.compile(r"[ \t\u3000]+")
_BLANK_RE = re.compile(r"\n{3,}")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style|head)[^>]*>.*?</\1>")
_BR_RE = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>")
_TIME_RE = re.compile(r"\d{1,2}[:：]\d{2}")
_DATE_RE = re.compile(r"\d{1,2}月\d{1,2}日|\d{4}[-/年]\d{1,2}[-/月]\d{1,2}")
_ENTITY_MAP = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&apos;": "'", "&mdash;": "-",
}

# 摘要优先挑选包含这些词的句子
SUMMARY_KEYWORDS: Sequence[str] = (
    "面试", "笔试", "测评", "机试", "考试", "时间", "日期", "地点", "地址",
    "链接", "网址", "会议", "zoom", "腾讯会议", "上午", "下午", "点",
    "offer", "录用", "入职", "岗位", "职位", "面试官", "hr",
)


def decode_mime_header(value: Optional[str]) -> str:
    """解码 RFC2047 编码的邮件头（如 =?utf-8?B?...?=）。"""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value).strip()


def html_to_text(html: str) -> str:
    """HTML 转纯文本。优先用 bs4，缺失时退化为正则清洗。"""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        return soup.get_text("\n")
    except Exception:
        text = _SCRIPT_RE.sub(" ", html)
        text = _BR_RE.sub("\n", text)
        text = _TAG_RE.sub(" ", text)
        for entity, char in _ENTITY_MAP.items():
            text = text.replace(entity, char)
        return text


def normalize_text(text: str) -> str:
    """统一换行、去掉每行首尾空白、压缩多余空行，但保留段落结构。"""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RE.sub("\n\n", text).strip()


def extract_body_text(msg: Message) -> str:
    """从邮件中提取正文：优先 text/plain，没有则把 text/html 转成文本。"""
    plain_parts: List[str] = []
    html_parts: List[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = (part.get_content_type() or "").lower()
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
        except Exception:
            continue
        if content_type == "text/plain":
            plain_parts.append(text)
        else:
            html_parts.append(text)

    if plain_parts:
        return normalize_text("\n".join(plain_parts))
    if html_parts:
        return normalize_text(html_to_text("\n".join(html_parts)))
    return ""


def build_summary(text: str, max_chars: int = 180, drop_patterns: Optional[Iterable[str]] = None) -> str:
    """生成摘要：挑出最可能含「时间/地点/链接」的句子，按原文顺序拼起来。"""
    if not text:
        return ""
    drops = [str(p).lower() for p in (drop_patterns or []) if str(p).strip()]
    scored = []
    for index, line in enumerate(text.split("\n")):
        line = line.strip()
        if len(line) < 4 or line.startswith(">"):
            continue
        low = line.lower()
        if any(pattern in low for pattern in drops):
            continue
        score = sum(1 for kw in SUMMARY_KEYWORDS if kw in low)
        if _TIME_RE.search(line):
            score += 3
        if _DATE_RE.search(line):
            score += 3
        if "http" in low:
            score += 2
        scored.append((score, index, line))

    if not scored:
        return text[:max_chars]

    scored.sort(key=lambda item: (-item[0], item[1]))
    picked: List[tuple] = []
    total = 0
    for score, index, line in scored:
        if score <= 0 and picked:
            break
        if picked and total + len(line) + 1 > max_chars:
            break
        picked.append((index, line))
        total += len(line) + 1
        if total >= max_chars:
            break

    picked.sort()
    joined = " ".join(line for _, line in picked).strip()
    if not joined:
        joined = text[:max_chars]
    return joined[:max_chars]


def parse_mail_date(value: Optional[str]) -> Optional[datetime]:
    """解析邮件 Date 头。"""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except Exception:
        return None


def fallback_message_id(
    account: str, uid_validity: Optional[int], uid: Optional[int], subject: str, sender: str
) -> str:
    """邮件没有 Message-ID 头时，用账户+UID+主题+发件人算一个稳定的伪 ID。

    稳定性很重要：同一封邮件在多次运行中必须得到同一个值，否则去重会失效。
    """
    raw = f"{account}|{uid_validity}|{uid}|{subject}|{sender}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return f"<no-message-id-{digest}@local>"