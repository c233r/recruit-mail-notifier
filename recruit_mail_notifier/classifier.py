# -*- coding: utf-8 -*-
"""邮件分类：人工规则 + 大模型的两级策略，并对低置信度结果标记「待确认」。

决策流程
--------
1. 规则打分：按 config/rules.json 里的 strong / weak / negative 关键词加权。
2. 按 config/model.ini 的 strategy 决定是否调用大模型：
     rules_only     只用规则
     rules_then_llm 规则置信度低于 llm_trigger_confidence 时才调用模型
     llm_only       全部交给模型（模型不可用时自动退回规则）
3. 冲突降级：命中排除词、或多个类别得分接近时，主动压低置信度。
4. 状态判定：
     命中汇总范围内的类别且置信度达标 -> new（等待每日汇总通知）
     置信度低于 pending_review_confidence 但邮件确有招聘信号 -> pending_review（待确认）
     其余 -> ignored
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .db import CATEGORY_UNKNOWN, STATUS_IGNORED, STATUS_NEW, STATUS_PENDING_REVIEW
from .mime_utils import build_summary

# 与求职强相关、默认需要汇总通知的类别
RELEVANT_CATEGORIES: Tuple[str, ...] = ("interview", "written_test")

_DEFAULT_COMPANY_SUFFIXES: Tuple[str, ...] = (
    "股份有限公司", "有限责任公司", "有限公司", "集团有限公司", "集团",
    "银行", "研究院", "研究所", "科技", "网络", "信息", "软件", "学院", "大学",
)

# 招聘平台 / 在线测评平台：它们是投递渠道，不是招聘方公司名
_PLATFORM_NAMES: Tuple[str, ...] = (
    "牛客", "北森", "智联", "前程无忧", "boss直聘", "猎聘", "moka", "海纳",
    "赛码", "gradescope", "hackerrank", "codility", "shl", "中华英才",
)

_SENDER_NOISE_RE = re.compile(
    r"(校园招聘|社会招聘|招聘团队|招聘中心|招聘组|人力资源部|人事部|校招|社招|"
    r"recruitment|talent|team|通知|no-?reply|noreply|hr)",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"[【\[]([^】\]]{2,24})[】\]]")
# 提示词分两类：动词类（后面冒号可有可无）和标签类（必须带冒号）。
# 如果标签类也允许没有冒号，「没有公司信息」这种普通句子就会被误判成公司名。
_HINT_RE = re.compile(
    r"(?:来自|入职|加入|就职于|隶属于)\s*[:：]?\s*"
    r"([\u4e00-\u9fa5A-Za-z0-9()（）·]{2,24})"
    r"|(?:公司名称|企业名称|公司|企业)\s*[:：]\s*"
    r"([\u4e00-\u9fa5A-Za-z0-9()（）·]{2,24})"
)
# 这些词本身不是公司名，命中了要丢弃
_GENERIC_COMPANY_WORDS = (
    "公司信息", "公司名称", "企业信息", "企业名称", "贵公司", "我公司",
    "该公司", "本公司", "注意事项", "相关信息", "详细信息",
    "招聘", "通知", "面试", "笔试", "测评", "机试", "内推", "校招", "社招",
    "春招", "秋招", "实习", "重要", "紧急", "提醒", "公告", "温馨提示",
)
_BOUNDARY_RE = re.compile(r"[，,。；;：:\s、（）()\[\]【】]|的|了|请|您|你")
_COMPANY_PATTERN_CACHE: Dict[Tuple[str, ...], "re.Pattern[str]"] = {}

# ---- 面试/笔试时间抽取用的正则 ----
_RE_ISO_DT = re.compile(
    r"(?P<y>20\d{2})-(?P<mo>\d{1,2})-(?P<d>\d{1,2})[ T](?P<h>\d{1,2})[:：](?P<mi>\d{2})"
)
_RE_SLASH_DATE = re.compile(r"(?P<y>20\d{2})[/.](?P<mo>\d{1,2})[/.](?P<d>\d{1,2})")
_RE_CN_FULL = re.compile(
    r"(?P<y>20\d{2})\s*年\s*(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"
    r"[^0-9\n]{0,12}?(?P<ampm>[上下]午)?\s*(?P<h>\d{1,2})\s*[:：时点]\s*(?P<mi>\d{1,2})"
)
_RE_CN_FULL_DATE = re.compile(r"(?P<y>20\d{2})\s*年\s*(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日")
_RE_CN_MD_TIME = re.compile(
    r"(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"
    r"[^0-9\n]{0,12}?(?P<ampm>[上下]午)?\s*(?P<h>\d{1,2})\s*[:：时点]\s*(?P<mi>\d{1,2})"
)
_RE_CN_MD_DATE = re.compile(r"(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日")


# ==========================================================================
# 结果对象
# ==========================================================================
@dataclass
class Classification:
    category: str = CATEGORY_UNKNOWN
    label: str = ""
    confidence: float = 0.0
    source: str = "rules"
    reasons: List[str] = field(default_factory=list)
    company: str = ""
    position: str = ""         # 岗位名称（模型给）
    location: str = ""         # 面试/笔试地点或线上形式（模型给）
    notify_time: str = ""      # 通知时间 = 邮件自身时间
    event_time: str = ""       # 面试/笔试时间（归一化）
    event_time_raw: str = ""   # 面试/笔试时间（原文摘录）
    deadline: str = ""         # 截止时间，决定汇总里的排序
    summary: str = ""
    status: str = STATUS_IGNORED
    needs_review: bool = False
    hit_count: int = 0

    def to_record(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "category_label": self.label,
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
            "reasons": list(self.reasons),
            "company": self.company,
            "position": self.position,
            "location": self.location,
            "notify_time": self.notify_time,
            "event_time": self.event_time or None,
            "event_time_raw": self.event_time_raw or None,
            "deadline": self.deadline or None,
            "summary": self.summary,
            "status": self.status,
        }


# ==========================================================================
# 公司名抽取
# ==========================================================================
def _company_pattern(suffixes: Sequence[str]) -> "re.Pattern[str]":
    key = tuple(suffixes)
    cached = _COMPANY_PATTERN_CACHE.get(key)
    if cached is None:
        alt = "|".join(re.escape(s) for s in sorted(set(suffixes), key=len, reverse=True))
        # 前缀用懒惰匹配，后缀允许连续重复，这样「腾讯科技有限公司」能被完整取出，
        # 同时不会把前面一整句中文都吞进去
        cached = re.compile(
            r"([\u4e00-\u9fa5A-Za-z0-9()（）·]{2,20}?(?:(?:%s)){1,3})" % alt
        )
        _COMPANY_PATTERN_CACHE[key] = cached
    return cached


def _trim_leading_clause(name: str, limit: int = 14) -> str:
    """匹配结果过长时，从最后一个断句处截断，去掉前面的叙述性文字。"""
    if len(name) <= limit:
        return name
    last = -1
    for match in _BOUNDARY_RE.finditer(name):
        last = match.end()
    if 0 < last < len(name) - 1:
        return name[last:]
    return name


# 公司名前常见的动词与客套话前缀。注意把长词排在前面，避免只切掉一个字。
_LEADING_NOISE_RE = re.compile(
    r"^(?:我们|您好|你好|诚挚|诚邀|邀请您|邀请|参加|来自|加入|入职|就职于|隶属于|"
    r"恭喜|感谢|关于|本次|此次|特此|现诚邀|贵公司|贵司|您参加|你参加|您|你|贵)"
)


def _tidy_company(name: str, stopwords: Sequence[str]) -> str:
    name = re.sub(r"^[\s\-—–,，:：、|/\\【】\[\]()（）]+", "", str(name or ""))
    name = re.sub(r"[\s\-—–,，:：、|/\\【】\[\]()（）]+$", "", name)
    name = _trim_leading_clause(name)
    # 反复剥掉前置动词：如「参加腾讯科技有限公司」->「腾讯科技有限公司」
    for _ in range(6):
        stripped = _LEADING_NOISE_RE.sub("", name).strip()
        if stripped == name:
            break
        name = stripped
    low = name.lower()
    for stopword in stopwords:
        if stopword and low.startswith(stopword):
            name = name[len(stopword):].strip()
            low = name.lower()
    return name.strip()


def _valid_company(name: str, suffixes: Sequence[str] = ()) -> bool:
    if not name or len(name) < 2 or len(name) > 30:
        return False
    if "@" in name or "http" in name.lower():
        return False
    if re.fullmatch(r"[\d\W_]+", name):
        return False
    if any(word in name for word in _GENERIC_COMPANY_WORDS):
        return False
    if suffixes:
        # 把公司后缀全部去掉后什么都不剩，说明抽到的只是「网络」「集团」这类通用词
        remainder = name
        for suffix in sorted((str(s) for s in suffixes), key=len, reverse=True):
            remainder = remainder.replace(suffix, "")
        if len(remainder.strip()) < 2:
            return False
    low = name.lower()
    return not any(platform in low for platform in _PLATFORM_NAMES)


def extract_company(subject: str, sender: str, body: str, rules: Optional[Dict[str, Any]] = None) -> str:
    """按可靠性从高到低尝试：主题/正文里的公司全称 -> 【】里的短名 -> 发件人显示名。

    这是启发式规则，难免有误；把 model.ini 的 enabled 打开后，
    模型返回的 company 可以作为补充。
    """
    cfg = (rules or {}).get("company") or {}
    suffixes = cfg.get("suffixes") or list(_DEFAULT_COMPANY_SUFFIXES)
    stopwords = [str(s).lower() for s in (cfg.get("stopwords") or [])]
    pattern = _company_pattern(list(suffixes))

    # 1) 【xxx】/ [xxx] 这种短名最可靠（招聘邮件几乎都这么写），所以放在最前面
    for source in (subject or "", body or ""):
        for match in _BRACKET_RE.finditer(source):
            name = _tidy_company(match.group(1), stopwords)
            if _valid_company(name, suffixes):
                return name

    # 2) 带公司后缀的全称，先主题后正文
    for source in (subject or "", body or ""):
        for match in pattern.finditer(source):
            name = _tidy_company(match.group(1), stopwords)
            if _valid_company(name, suffixes):
                return name

    # 3) 「来自 / 公司：」这类提示词后面跟的名字
    for source in (subject or "", body or ""):
        for match in _HINT_RE.finditer(source):
            name = _tidy_company(match.group(1) or match.group(2) or "", stopwords)
            if _valid_company(name, suffixes):
                return name

    # 4) 最后退回发件人显示名（去掉「招聘 / HR」等噪声）
    display = parseaddr(sender or "")[0] or ""
    display = _SENDER_NOISE_RE.sub(" ", display)
    display = re.sub(r"[\s\[\]【】()（）,，、|]+", " ", display).strip()
    if _valid_company(display, suffixes):
        return display
    return ""


# ==========================================================================
# 面试/笔试时间抽取
# ==========================================================================
def _build_datetime(match: "re.Match[str]", tz: Any, now: datetime) -> Optional[datetime]:
    groups = match.groupdict()
    try:
        year = int(groups["y"]) if groups.get("y") else now.year
        month = int(groups.get("mo") or 1)
        day = int(groups.get("d") or 1)
        hour = int(groups.get("h") or 0)
        minute = int(groups.get("mi") or 0)
    except (TypeError, ValueError):
        return None

    ampm = groups.get("ampm")
    if ampm == "下午" and hour < 12:
        hour += 12
    elif ampm == "上午" and hour == 12:
        hour = 0

    if not (1 <= month <= 12 and 1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    try:
        moment = datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError:
        return None

    # 只写了「x月x日」且推算出的日期已经过去时，按明年的安排处理
    if not groups.get("y") and moment < now - timedelta(days=1):
        try:
            moment = moment.replace(year=moment.year + 1)
        except ValueError:
            pass
    return moment


def extract_event_time(
    text: str, tz: Any, now: Optional[datetime] = None
) -> Tuple[Optional[str], str]:
    """从正文里抽面试/笔试的具体时间，返回 (ISO 字符串或 None, 原文片段)。"""
    if not text:
        return None, ""
    now = now or datetime.now(tz)
    for pattern in (
        _RE_ISO_DT, _RE_CN_FULL, _RE_CN_MD_TIME,
        _RE_SLASH_DATE, _RE_CN_FULL_DATE, _RE_CN_MD_DATE,
    ):
        for match in pattern.finditer(text):
            raw = match.group(0).strip()
            moment = _build_datetime(match, tz, now)
            iso = moment.replace(microsecond=0).isoformat() if moment else None
            # 明显不合理的日期（版权年份、模板时间戳）直接跳过，继续往后找
            if _within_window(iso, tz, now):
                return iso, raw
    return None, ""


def _notify_time_local(received_at: Optional[str], tz: Any, now: datetime) -> str:
    """通知时间：优先用邮件自身的 Date 头，缺失时退化为当前时间。"""
    if received_at:
        try:
            moment = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(tz).replace(microsecond=0).isoformat()
        except ValueError:
            pass
    return now.replace(microsecond=0).isoformat()


# ==========================================================================
# 截止时间抽取（决定每日汇总的排序）
# ==========================================================================
_DEFAULT_DEADLINE_KEYWORDS: Tuple[str, ...] = (
    "截止时间", "截止日期", "报名截止", "申请截止", "截止",
    "DDL", "ddl", "deadline",
    "请于", "请在", "最晚", "务必于", "需于", "要在", "需要在",
    "前完成", "前提交", "前回复", "前确认", "前答复",
    "之前完成", "之前回复", "之前确认",
)
_DEADLINE_DATE_PATTERNS = (
    _RE_ISO_DT, _RE_CN_FULL, _RE_CN_MD_TIME, _RE_SLASH_DATE, _RE_CN_FULL_DATE, _RE_CN_MD_DATE,
)


def _deadline_keyword_re(rules: Optional[Dict[str, Any]] = None) -> "re.Pattern[str]":
    cfg = (rules or {}).get("deadline") or {}
    words = cfg.get("keywords") or _DEFAULT_DEADLINE_KEYWORDS
    unique = sorted({str(w).strip() for w in words if str(w).strip()}, key=len, reverse=True)
    alt = "|".join(re.escape(word) for word in unique)
    return re.compile(alt, re.IGNORECASE)


def _pick_datetime(window: str, tz: Any, now: datetime, last: bool = False) -> Tuple[Optional[str], str]:
    """在片段里找日期时间。同一起点保留最长匹配，避免丢掉时分。"""
    best: Dict[int, Any] = {}
    for pattern in _DEADLINE_DATE_PATTERNS:
        for match in pattern.finditer(window):
            current = best.get(match.start())
            if current is None or len(match.group(0)) > len(current.group(0)):
                best[match.start()] = match
    if not best:
        return None, ""
    ordered = [best[key] for key in sorted(best)]
    match = ordered[-1] if last else ordered[0]
    raw = match.group(0).strip()
    moment = _build_datetime(match, tz, now)
    return (moment.replace(microsecond=0).isoformat() if moment else None), raw


def extract_deadline(
    text: str, tz: Any, now: Optional[datetime] = None, rules: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[str], str]:
    """在「截止 / 请于 / 前完成」这类说法附近找日期时间。

    先看关键词后面 100 个字符；找不到再回头看前面 60 个字符，
    以覆盖「3月10日 前完成」这种把日期写在关键词前面的写法。
    """
    if not text:
        return None, ""
    now = now or datetime.now(tz)
    for match in _deadline_keyword_re(rules).finditer(text):
        # 每个关键词先看后面、再看前面；候选日期不合理的就跳过，继续找下一个
        for window, take_last in (
            (text[match.end(): match.end() + 100], False),
            (text[max(0, match.start() - 60): match.start()], True),
        ):
            iso, raw = _pick_datetime(window, tz, now, take_last)
            if raw and _within_window(iso, tz, now):
                return iso, raw
    return None, ""


# ==========================================================================
# 规则分类器
# ==========================================================================
class RuleClassifier:
    """基于关键词加权的规则分类器。参数全部来自 config/rules.json。"""

    def __init__(self, rules: Optional[Dict[str, Any]], classifier_cfg: Any, logger: Any = None) -> None:
        self.rules = rules or {}
        self.cfg = classifier_cfg
        self.logger = logger
        self.categories: Dict[str, Any] = self.rules.get("categories") or {}
        scoring = self.rules.get("scoring") or {}
        self.w_strong = float(scoring.get("strong_hit", 0.45))
        self.w_weak = float(scoring.get("weak_hit", 0.18))
        self.w_subject = float(scoring.get("subject_bonus", 0.20))
        self.w_negative = float(scoring.get("negative_hit", 0.50))
        self.max_confidence = float(scoring.get("max_confidence", 0.98))
        self.summary_cfg = self.rules.get("summary") or {}

    def label_of(self, category: str) -> str:
        spec = self.categories.get(category) or {}
        return spec.get("label") or category

    @staticmethod
    def _hits(text_low: str, keywords: Sequence[str]) -> List[str]:
        found = []
        for keyword in keywords or []:
            keyword = str(keyword).strip()
            if keyword and keyword.lower() in text_low:
                found.append(keyword)
        return found

    def scores(self, subject: str, body: str) -> Dict[str, Dict[str, Any]]:
        subject_low = (subject or "").lower()
        combined = subject_low + "\n" + (body or "").lower()
        result: Dict[str, Dict[str, Any]] = {}
        for name, spec in self.categories.items():
            strong = self._hits(combined, spec.get("strong") or [])
            weak = [k for k in self._hits(combined, spec.get("weak") or []) if k not in strong]
            negative = self._hits(combined, spec.get("negative") or [])
            score = len(strong) * self.w_strong + len(weak) * self.w_weak
            if self._hits(subject_low, spec.get("strong") or []):
                score += self.w_subject
            elif self._hits(subject_low, spec.get("weak") or []):
                score += self.w_subject * 0.5
            score -= len(negative) * self.w_negative
            result[name] = {
                "score": max(0.0, min(self.max_confidence, score)),
                "strong": strong,
                "weak": weak,
                "negative": negative,
                "label": spec.get("label") or name,
                "hits": len(strong) + len(weak),
            }
        return result

    def classify(self, subject: str, sender: str, body: str) -> Classification:
        scores = self.scores(subject, body)
        ordered = sorted(scores.items(), key=lambda kv: kv[1]["score"], reverse=True)
        if not ordered:
            return Classification(reasons=["rules.json 里没有配置任何类别"])

        top_name, top = ordered[0]
        runner_up = ordered[1] if len(ordered) > 1 else ("", {"score": 0.0, "label": ""})
        confidence = float(top["score"])
        hit_count = int(top["hits"])
        reasons: List[str] = []

        if top["strong"]:
            reasons.append("命中强关键词：" + "、".join(top["strong"][:6]))
        if top["weak"]:
            reasons.append("命中弱关键词：" + "、".join(top["weak"][:6]))
        if top["negative"]:
            reasons.append("命中排除词：" + "、".join(top["negative"][:6]))

        if confidence <= 0:
            return Classification(
                category=CATEGORY_UNKNOWN, label="", confidence=0.0,
                source="rules", reasons=reasons or ["未命中任何关键词"],
                hit_count=hit_count,
            )

        # 得分接近 -> 有歧义，压低置信度交给模型或人工
        if runner_up[1]["score"] > 0 and (confidence - float(runner_up[1]["score"])) < self.cfg.ambiguity_margin:
            confidence = round(confidence * 0.6, 4)
            reasons.append(f"与「{runner_up[1].get('label') or runner_up[0]}」得分接近，置信度下调")

        # 命中排除词 -> 更保守
        if top["negative"]:
            confidence = round(confidence * 0.5, 4)
            reasons.append("命中排除词，置信度下调")

        return Classification(
            category=top_name,
            label=str(top.get("label") or top_name),
            confidence=confidence,
            source="rules",
            reasons=reasons,
            hit_count=hit_count,
        )


# ==========================================================================
# 模型调用配额
# ==========================================================================
class LlmBudget:
    """按天限制模型调用次数，防止规则失效时把额度烧光。limit<=0 表示不限。"""

    def __init__(self, db: Any = None, daily_limit: int = 0, logger: Any = None) -> None:
        self.db = db
        self.limit = int(daily_limit or 0)
        self.logger = logger

    def allow(self) -> bool:
        if self.db is None or self.limit <= 0:
            return True
        try:
            used = int(self.db.llm_calls_today())
        except Exception:
            return True
        if used >= self.limit:
            if self.logger:
                self.logger.warning("今日模型调用已达上限 %d，本轮跳过模型分类", self.limit)
            return False
        return True

    def consume(self, amount: int = 1) -> None:
        if self.db is None:
            return
        try:
            self.db.bump_llm_calls(amount=amount)
        except Exception:
            pass


# ==========================================================================
# 只看主题的快速判定
# ==========================================================================
def match_subject_rule(
    subject: str, rules: Optional[Dict[str, Any]] = None
) -> Optional[Tuple[str, str]]:
    """只看主题判断是不是面试/笔试/测评通知。

    招聘通知的主题几乎一定写着「面试」「笔试」「测评」；而正文里这些词大量出现在
    免责声明、公司介绍、别的公司名和退订说明里，一起匹配会产生大量误判，
    所以默认只在主题上做规则匹配。返回 (类别, 命中的关键词)，没命中返回 None。
    """
    if not subject:
        return None
    spec = (rules or {}).get("subject_rules") or {}
    subject_low = subject.lower()

    # 明确的非通知类（满意度问卷、感谢信、取消通知）不做快速判定，交给模型/兜底
    for word in spec.get("negative") or []:
        word = str(word).strip()
        if word and word.lower() in subject_low:
            return None

    best: Optional[Tuple[int, str, str]] = None
    for category in ("written_test", "interview"):
        for keyword in spec.get(category) or []:
            keyword = str(keyword).strip()
            if not keyword:
                continue
            position = subject_low.find(keyword.lower())
            if position < 0:
                continue
            # 同时命中多个关键词时，取在主题里出现得更靠前的那个
            if best is None or position < best[0]:
                best = (position, category, keyword)
    if best is None:
        return None
    return best[1], best[2]


def _reference_time(received_at: Optional[str], tz: Any, fallback: datetime) -> datetime:
    """日期解析的参考时间：优先用邮件收到的时间。

    补跑历史邮件时这点很关键 —— 如果用「当前时间」推算年份，
    9 月收到的、写着「9月26日」的通知会被算到明年去。
    """
    if received_at:
        try:
            moment = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return moment.astimezone(tz)
        except ValueError:
            pass
    return fallback


def _sane_iso(value: Any, tz: Any, reference: datetime) -> Optional[str]:
    """校验模型直接给出的 ISO 时间：能解析、且落在合理区间内才算数。

    模型有时会编造时间或写成「昨天下午」这种，这里一律判为不可用，
    交给后面的正则归一化兜底。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    iso = moment.replace(microsecond=0).isoformat()
    return iso if _within_window(iso, tz, reference) else None


def _within_window(
    iso_value: Optional[str],
    tz: Any,
    reference: datetime,
    past_days: int = 90,
    future_days: int = 400,
) -> bool:
    """过滤明显不合理的日期。

    邮件正文里常混着版权年份、模板时间戳，会解析出「2000-04-20」这种
    根本不可能是截止时间的值，必须丢掉，否则排序和「还有几天」全是错的。
    """
    if not iso_value:
        return False
    try:
        moment = datetime.fromisoformat(str(iso_value))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    delta_days = (moment - reference).total_seconds() / 86400.0
    return -past_days <= delta_days <= future_days


# ==========================================================================
# 时间片段归一化
# ==========================================================================
def normalize_datetime_text(
    text: str, tz: Any, reference: datetime, treat_as_deadline: bool = False
) -> Tuple[Optional[str], str]:
    """把模型摘出来的时间原文片段归一化成 ISO 时间。

    分工：模型负责「找出邮件里跟时间有关的那句话」，正则负责把它变成标准时间。
    这样补跑历史邮件时年份也不会算错（参考时间用的是邮件收到时间）。
    """
    if not text:
        return None, ""
    if treat_as_deadline:
        iso, raw = extract_deadline(text, tz, reference)
        if iso:
            return iso, raw
    iso, raw = extract_event_time(text, tz, reference)
    if iso:
        return iso, raw
    return None, ""


# ==========================================================================
# 对外主入口
# ==========================================================================
def classify_mail(
    mail: Any,
    cfg: Any,
    llm_client: Any = None,
    budget: Optional[LlmBudget] = None,
    now: Optional[datetime] = None,
    logger: Any = None,
) -> Classification:
    """对一封 RawMail 做分类并抽取字段，返回可直接写库的 Classification。

    三种策略：
      llm_only        全部交给大模型，规则完全不参与（模型按固定 JSON 返回字段）
      rules_then_llm  主题命中「笔试/测评/面试」直接判定，否则交给模型
      rules_only      只用规则，不调用模型

    模型不可用或调用失败时，统一降级为「规则粗判 + 待确认」，保证离线也能跑。
    """
    tz = cfg.tzinfo(logger)
    now = now or datetime.now(tz)
    # 年份推算要用「邮件收到的时间」，不能用当前时间（补跑历史邮件会算错年份）
    reference = _reference_time(getattr(mail, "received_at", None), tz, now)

    rule_clf = RuleClassifier(cfg.rules, cfg.classifier, logger)
    scope = str(getattr(cfg.classifier, "rule_scope", "subject") or "subject").lower()
    strategy = str(getattr(cfg.classifier, "strategy", "rules_then_llm") or "rules_then_llm").lower()
    model_ready = llm_client is not None and cfg.model.is_ready()

    result = Classification()
    force_review = False

    # 模型可能给出的、需要单独接管的字段
    llm_company = llm_position = llm_location = ""
    llm_event_text = llm_deadline_text = llm_summary = ""
    llm_event_iso = llm_deadline_iso = ""

    # 主题快判只在「规则参与」的策略下才有意义
    subject_hit = None
    if strategy != "llm_only" and scope == "subject":
        subject_hit = match_subject_rule(mail.subject, cfg.rules)

    # 规则粗判：既作为 rules_then_llm 的判定依据，也作为模型不可用时的兜底
    fallback = None
    if strategy != "llm_only" and subject_hit is None:
        fallback = rule_clf.classify(mail.subject, mail.sender, mail.body_text)

    # ---------------- 第一级：类别与置信度 ----------------
    need_llm = False
    if strategy == "llm_only":
        need_llm = True
    elif subject_hit is not None:
        need_llm = False
    elif scope == "subject":
        need_llm = True
    elif strategy == "rules_then_llm":
        need_llm = fallback is not None and fallback.confidence < cfg.classifier.llm_trigger_confidence

    if need_llm and not model_ready:
        force_review = True
        reason = ("strategy=llm_only 但大模型未就绪" if strategy == "llm_only"
                  else "大模型未就绪")
        result.reasons.append("%s，退回规则粗判并标记待确认" % reason)
        if logger and strategy == "llm_only":
            logger.warning("%s，无法按模型判断：%s", reason, (mail.subject or "")[:50])
        need_llm = False
    elif need_llm and budget is not None and not budget.allow():
        force_review = True
        result.reasons.append("今日模型调用已达上限，退回规则粗判并标记待确认")
        need_llm = False

    if need_llm:
        llm_result = llm_client.classify(
            mail.subject,
            mail.sender,
            mail.body_text,
            received_local=reference.strftime("%Y-%m-%d %H:%M:%S%z"),
            tz_name=str(getattr(cfg.notify, "timezone", "") or ""),
        )
        if budget is not None:
            budget.consume()
        if llm_result.ok:
            result.category = llm_result.category
            result.label = rule_clf.label_of(llm_result.category)
            result.confidence = llm_result.confidence
            result.source = "llm"
            result.reasons.append("模型判定：%s" % (llm_result.reason or llm_result.category))
            if not llm_result.is_job_notification:
                result.reasons.append("模型判断这不是求职类通知")
            result.hit_count = (
                1 if (llm_result.category != CATEGORY_UNKNOWN and llm_result.is_job_notification) else 0
            )
            llm_company = llm_result.company
            llm_position = llm_result.position
            llm_location = llm_result.location
            llm_event_text = llm_result.event_time_text
            llm_event_iso = getattr(llm_result, "event_time", "") or ""
            llm_deadline_text = llm_result.deadline_text
            llm_deadline_iso = getattr(llm_result, "deadline", "") or ""
            llm_summary = llm_result.summary
        else:
            result.reasons.append("模型调用失败：%s" % llm_result.error)
            if logger:
                logger.error("模型分类失败：%s", llm_result.error)
            if cfg.model.fallback_to_pending_review:
                force_review = True

    # ---------------- 类别兜底 ----------------
    if result.source == "llm":
        pass
    elif subject_hit is not None:
        category, keyword = subject_hit
        result.category = category
        result.label = rule_clf.label_of(category)
        result.confidence = float(getattr(cfg.classifier, "subject_match_confidence", 0.95))
        result.source = "rules"
        result.hit_count = 1
        result.reasons.append(
            "主题命中「%s」-> 直接判定为%s，不调用模型" % (keyword, result.label)
        )
    elif fallback is not None:
        result.category = fallback.category
        result.label = fallback.label
        result.confidence = fallback.confidence
        result.source = fallback.source
        result.hit_count = fallback.hit_count
        result.reasons = list(result.reasons) + list(fallback.reasons)
    elif strategy == "llm_only":
        # 模型不可用，规则也不参与：直接兜底跑一次全文规则，只用来分流
        fallback = rule_clf.classify(mail.subject, mail.sender, mail.body_text)
        result.category = fallback.category
        result.label = fallback.label
        result.confidence = fallback.confidence
        result.source = "rules"
        result.hit_count = fallback.hit_count
        result.reasons = list(result.reasons) + list(fallback.reasons)

    # ---------------- 第二级：公司 / 岗位 / 地点 / 时间 / 摘要 ----------------
    combined_text = (mail.subject or "") + "\n" + (mail.body_text or "")

    # 模型给的公司名比规则可靠（规则会抽出「工作地点」这类正文噪声），优先用模型的
    result.company = llm_company or extract_company(
        mail.subject, mail.sender, mail.body_text, cfg.rules
    )
    result.position = llm_position
    result.location = llm_location

    # 面试/笔试时间：优先用模型直接推算出的 ISO（它能处理「48小时内」这类
    # 正文里的相对说法），模型没给或给得不合法时，才退回「摘录原文 + 正则归一化」。
    event_iso = _sane_iso(llm_event_iso, tz, reference)
    event_raw = llm_event_text
    if not event_iso:
        event_iso, event_raw = normalize_datetime_text(llm_event_text, tz, reference)
    if not event_iso:
        event_iso, event_raw = extract_event_time(combined_text, tz, reference)
    result.event_time = event_iso or ""
    result.event_time_raw = event_raw or llm_event_text

    deadline_iso = _sane_iso(llm_deadline_iso, tz, reference)
    if not deadline_iso:
        deadline_iso, _deadline_raw = normalize_datetime_text(
            llm_deadline_text, tz, reference, treat_as_deadline=True
        )
    if not deadline_iso:
        deadline_iso, _ = extract_deadline(combined_text, tz, reference, cfg.rules)
    # 没有明确截止时间就用面试/笔试时间兜底（那本身就是最晚要处理的时间点）
    result.deadline = deadline_iso or result.event_time

    result.notify_time = _notify_time_local(getattr(mail, "received_at", None), tz, now)

    if llm_summary:
        result.summary = llm_summary[:300]
    else:
        summary_cfg = cfg.rules.get("summary") or {}
        result.summary = build_summary(
            mail.body_text or "",
            int(summary_cfg.get("max_chars", 180) or 180),
            summary_cfg.get("drop_lines_matching") or [],
        )

    # ---------------- 状态判定 ----------------
    threshold = float(cfg.classifier.pending_review_confidence)
    relevant = set(cfg.notify.categories or RELEVANT_CATEGORIES)
    has_signal = result.hit_count > 0 or result.category != CATEGORY_UNKNOWN

    if force_review:
        result.status = STATUS_PENDING_REVIEW if has_signal else STATUS_IGNORED
        if result.status == STATUS_PENDING_REVIEW:
            result.reasons.append("标记为待确认，等人工或模型复核")
    elif result.category != CATEGORY_UNKNOWN and result.confidence >= threshold:
        if result.category in relevant:
            result.status = STATUS_NEW
        else:
            result.status = STATUS_IGNORED
            result.reasons.append("类别 %s 不在汇总范围内，仅入库不通知" % result.category)
    else:
        result.status = STATUS_PENDING_REVIEW if has_signal else STATUS_IGNORED
        if result.status == STATUS_PENDING_REVIEW:
            result.reasons.append(
                "置信度 %.2f 低于阈值 %.2f，标记为待确认" % (result.confidence, threshold)
            )

    result.needs_review = result.status == STATUS_PENDING_REVIEW
    if not result.label:
        result.label = rule_clf.label_of(result.category)
    return result
