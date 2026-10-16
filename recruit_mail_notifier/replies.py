# -*- coding: utf-8 -*-
"""识别并解析「完成回复」邮件。

用法：收到每天的汇总邮件后直接回复它，在正文里写上已经完成的公司名，例如：

    腾讯
    字节跳动

或者写「全部完成」。系统下一轮收信时会把这些记录标记为已完成，
第二天它们就不再出现在提醒里，没写完的继续提醒。

解析上最关键的一点：必须先把引用的原文切掉。汇总邮件正文里本身就有公司名，
如果回复里带着整篇原文而我们没切干净，就会误判成「所有公司都完成了」。
所以这里做了三层防护：切引用分隔符、丢引用行、丢本系统自己的排版行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

_REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|回复|答复|转发)\s*[:：]\s*)+", re.IGNORECASE)
_SUMMARY_MARK_RE = re.compile(r"招聘汇总|面试汇总|笔试汇总")

# 引用原文的起点：从这里开始的内容全部丢弃
# 注意用 (?:\A|\n)：回复正文可能一开头就是引用（用户没写任何新内容），
# 这时候必须从第 0 个字符就切断，否则整篇汇总会被当成用户的输入。
_QUOTE_CUT_PATTERNS: Sequence[re.Pattern] = (
    re.compile(r"(?:\A|\n)\s*在\s*20\d{2}[^\n]{0,80}写道\s*[:：]"),
    re.compile(r"(?:\A|\n)\s*on\s+[^\n]{0,120}wrote\s*[:：]", re.IGNORECASE),
    re.compile(r"(?:\A|\n)-{2,}\s*(?:原始邮件|原始郵件|original message|forwarded message)", re.IGNORECASE),
    re.compile(r"(?:\A|\n)_{5,}"),
    re.compile(r"(?:\A|\n)\s*发件人\s*[:：]"),
    re.compile(r"(?:\A|\n)\s*from\s*[:：]", re.IGNORECASE),
    re.compile(r"(?:\A|\n)\s*={5,}"),
    re.compile(r"(?:\A|\n)\s*>{1,}"),
)

# 这些行是本系统自己发出的汇总邮件的排版，一律当引用丢掉
_OWN_FORMAT_PATTERNS: Sequence[re.Pattern] = (
    re.compile(r"^\s*\d+\s*[.、)]\s*【"),
    re.compile(
        r"^\s*(?:主题|通知时间|截止时间|面试时间|笔试时间|摘要|邮件链接|类别|公司|岗位|地点)\s*[:：]"
    ),
    re.compile(r"^\s*[—\-=*]{3,}\s*$"),
    re.compile(r"^\s*招聘邮件汇总"),
    re.compile(r"^\s*共\s*\d+\s*封"),
    re.compile(
        r"^\s*(?:待确认|本邮件由|可用命令|今天没有|直接回复|系统下一轮|系统明天|"
        r"在正文里|写「|回复本邮件)"
    ),
    re.compile(r"^\s*[·•]"),
)

_DEFAULT_COMPLETION_WORDS: Sequence[str] = (
    "已完成", "完成", "搞完", "搞定", "结束", "面完", "笔完", "考完",
    "通过了", "通过", "已拒", "拒了", "不去了", "放弃了", "done", "ok",
)
_DEFAULT_ALL_WORDS: Sequence[str] = (
    "全部完成", "全部搞定", "全搞定", "都完成", "都搞定", "所有都", "以上都", "all done",
)
# 「#12」「#12 #15」「编号 12」「id:12」都算明确指定某一条记录
_ID_RE = re.compile(r"[#＃]\s*(\d{1,9})|(?:编号|序号|id)\s*[:：]?\s*(\d{1,9})", re.IGNORECASE)

# 回复签名里常见的噪声：邮箱、网址、HTML 实体、邮件客户端自动加的落款。
# 不滤掉的话，「No&nbsp;name」会被拆成 no / name，而 no 这种两字母词
# 可能跟 "Nokia" 之类的公司名误匹配。
_SIGNATURE_NOISE = frozenset({"no", "name", "none", "noreply", "signature", "sent"})


def _is_noise_token(token: str) -> bool:
    low = str(token or "").strip().lower()
    if len(low) < 2 or low.isdigit():
        return True
    if any(ch in low for ch in "@<>"):
        return True
    if "http" in low or "&nbsp" in low or "&amp" in low or ".com" in low:
        return True
    return low in _SIGNATURE_NOISE

OWN_SUMMARY = "own_summary"
COMPLETION_REPLY = "reply"
OTHER = "other"

_DEFAULT_COMPANY_SUFFIXES: Sequence[str] = (
    "股份有限公司", "有限责任公司", "有限公司", "集团有限公司", "集团",
    "银行", "研究院", "研究所", "科技", "网络", "信息", "软件", "学院", "大学",
)


@dataclass
class CompletionReply:
    """一封完成回复解析出来的内容。"""

    body: str = ""
    mentions: List[str] = field(default_factory=list)
    ids: List[int] = field(default_factory=list)
    mark_all: bool = False
    has_completion_word: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.mark_all and not self.mentions


def _row_id(row: Any) -> Optional[int]:
    try:
        return int(_get(row, "id", None))
    except (TypeError, ValueError):
        return None


def _extract_ids(cleaned: str) -> List[int]:
    """从回复里取出明确的记录编号（#12 / 编号 12 / id:12）。"""
    found: List[int] = []
    for match in _ID_RE.finditer(cleaned or ""):
        token = match.group(1) or match.group(2)
        if token:
            found.append(int(token))
    seen = set()
    unique: List[int] = []
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _get(row: Any, key: str, default: Any = "") -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        value = default
    return default if value is None else value


def classify_incoming(
    subject: str,
    in_reply_to: str = "",
    references: str = "",
    body_text: str = "",
    from_display: str = "",
    from_name: str = "",
) -> str:
    """判断收到的邮件属于哪一类：系统自己的汇总 / 用户的完成回复 / 其他。

    为什么必须区分：汇总邮件是发到本邮箱的（收件人就是自己），
    所以下一轮拉取会把它自己再收回来。要是不区分，它会被当成「完成回复」处理，
    解析出 0 个公司名白跑一趟，还会在数据库里留下一堆假记录。

    判定依据（按可靠性排序）：
      1. 有 In-Reply-To / References  -> 是在回复某封邮件；
      2. 主题带「回复：/Re:」前缀     -> 也是在回复；
      3. 主题像汇总但没有回复前缀     -> 就是系统自己发出去的那封。
    """
    subject = subject or ""
    headers = "%s %s" % (in_reply_to or "", references or "")
    is_summary_subject = bool(_SUMMARY_MARK_RE.search(subject))
    has_reply_prefix = bool(_REPLY_PREFIX_RE.match(subject))

    if headers.strip():
        return COMPLETION_REPLY if is_summary_subject else OTHER
    if has_reply_prefix:
        return COMPLETION_REPLY if is_summary_subject else OTHER
    if is_summary_subject:
        return OWN_SUMMARY
    return OTHER


def looks_like_summary_reply(
    subject: str,
    in_reply_to: str = "",
    references: str = "",
    known_message_ids: Iterable[str] = (),
) -> bool:
    """判断这封邮件是不是在回复我们发出去的汇总邮件。

    注意：QQ 邮箱转发时会**重写 Message-ID**，所以我们生成的 ID 到对方邮箱里
    就对不上了，known_message_ids 只能作为辅助线索，主要靠主题判断。
    """
    if classify_incoming(subject, in_reply_to, references) == COMPLETION_REPLY:
        return True

    headers = "%s %s" % (in_reply_to or "", references or "")
    for message_id in known_message_ids or ():
        message_id = str(message_id or "").strip()
        if message_id and message_id in headers:
            return True
    return False


def strip_quoted(text: str) -> str:
    """只保留用户新写的内容，把引用的原文和本系统的排版行全部去掉。"""
    if not text:
        return ""
    cut = len(text)
    for pattern in _QUOTE_CUT_PATTERNS:
        match = pattern.search(text)
        if match and match.start() < cut:
            cut = match.start()

    kept: List[str] = []
    for line in text[:cut].split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if any(pattern.match(line) for pattern in _OWN_FORMAT_PATTERNS):
            continue
        kept.append(stripped)
    return "\n".join(kept)


def _completion_words(rules: Optional[Dict[str, Any]]) -> List[str]:
    cfg = (rules or {}).get("completion") or {}
    words = cfg.get("words") or _DEFAULT_COMPLETION_WORDS
    return [str(w) for w in words if str(w).strip()]


def _all_words(rules: Optional[Dict[str, Any]]) -> List[str]:
    cfg = (rules or {}).get("completion") or {}
    words = cfg.get("all_words") or _DEFAULT_ALL_WORDS
    return [str(w) for w in words if str(w).strip()]


def _extract_mentions(cleaned: str, words: Sequence[str]) -> List[str]:
    mentions: List[str] = []
    for line in cleaned.split("\n"):
        line = line.strip()
        if len(line) < 2:
            continue
        line = re.sub(r"^\s*[-*•\d]+\s*[.、)）]?\s*", "", line)
        for word in words:
            line = re.sub(re.escape(word), " ", line, flags=re.IGNORECASE)
        line = re.sub(r"[，,。；;、:：!！?？/\\|#＃]+", " ", line)
        for token in line.split():
            token = token.strip()
            if _is_noise_token(token):
                continue
            mentions.append(token)

    seen = set()
    unique: List[str] = []
    for item in mentions:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def parse_completion_body(text: str, rules: Optional[Dict[str, Any]] = None) -> CompletionReply:
    """解析回复正文，得到候选公司名和「全部完成」指令。"""
    words = _completion_words(rules)
    all_words = _all_words(rules)

    cleaned = strip_quoted(text)
    low = cleaned.lower()
    reply = CompletionReply(body=cleaned)
    reply.mark_all = any(word.lower() in low for word in all_words)
    reply.has_completion_word = reply.mark_all or any(word.lower() in low for word in words)
    reply.ids = _extract_ids(cleaned)
    reply.mentions = _extract_mentions(cleaned, words)
    return reply


def company_core(name: str, rules: Optional[Dict[str, Any]] = None) -> str:
    """把「腾讯科技有限公司」压成「腾讯」，好跟用户手写的简称对上。"""
    text = str(name or "").strip()
    cfg = (rules or {}).get("company") or {}
    suffixes = cfg.get("suffixes") or list(_DEFAULT_COMPANY_SUFFIXES)
    for suffix in sorted((str(s) for s in suffixes), key=len, reverse=True):
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[: -len(suffix)]
            break
    for tail in ("股份", "科技", "网络", "信息", "软件", "技术", "集团", "公司"):
        if text.endswith(tail) and len(text) > len(tail) + 1:
            text = text[: -len(tail)]
    return text.strip()


def match_pending_companies(
    reply: CompletionReply, pending_rows: Sequence[Any], rules: Optional[Dict[str, Any]] = None
) -> List[Any]:
    """把回复里提到的内容对上数据库里未完成的记录。

    回复里写了明确编号（#12）时**只按编号匹配**：同一家公司可能有多场笔试/面试，
    按公司名匹配会把这家公司的记录一次性全部标掉，那不是用户想要的。
    """
    if reply.ids:
        wanted = {int(item) for item in reply.ids}
        return [row for row in pending_rows if _row_id(row) in wanted]

    haystack = (reply.body or "").lower()
    wanted = [m.lower() for m in reply.mentions if len(m) >= 2]
    matched: List[Any] = []

    for row in pending_rows:
        company = str(_get(row, "company") or "").strip()
        if not company:
            continue
        candidates = {company}
        core = company_core(company, rules)
        if len(core) >= 2:
            candidates.add(core)

        for candidate in candidates:
            low = candidate.lower()
            if not low:
                continue
            if low in haystack or any(low in item or item in low for item in wanted):
                matched.append(row)
                break
    return matched
