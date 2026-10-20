# -*- coding: utf-8 -*-
"""配置加载：mail.ini（邮件）+ model.ini（模型）+ 环境变量（凭据）。

设计要点
--------
1. 邮件配置与模型配置拆成两个 INI，互不影响，方便按需动态调整。
2. load_config() 每次调用都重新读盘；定时任务每轮都会重新加载，
   因此改完 INI 保存后无需重启进程即可生效。
3. 凭据（邮箱授权码、API Key）只从环境变量读取，INI 里只写变量名，
   代码中不出现任何真实账号密码。环境变量的改动需重启进程。
4. Windows 上 zoneinfo 可能缺 IANA 时区库（缺 tzdata 包），
   这里内置常用时区的固定偏移降级方案，保证程序仍能按正确时刻运行。
"""

from __future__ import annotations

import configparser
import json
import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
MAIL_INI = CONFIG_DIR / "mail.ini"
MODEL_INI = CONFIG_DIR / "model.ini"
RULES_JSON = CONFIG_DIR / "rules.json"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "recruit_mail.db"
DEFAULT_LOG_DIR = PROJECT_ROOT / "logs"

_ENV_LOADED = False

# zoneinfo 不可用（未安装 tzdata）时的降级表，单位为小时。
_FALLBACK_OFFSETS: Dict[str, float] = {
    "UTC": 0.0, "Etc/UTC": 0.0, "GMT": 0.0,
    "Asia/Shanghai": 8.0, "Asia/Chongqing": 8.0, "Asia/Harbin": 8.0,
    "Asia/Hong_Kong": 8.0, "Asia/Macau": 8.0, "Asia/Taipei": 8.0,
    "Asia/Singapore": 8.0, "Asia/Kuala_Lumpur": 8.0,
    "Asia/Tokyo": 9.0, "Asia/Seoul": 9.0,
    "Europe/London": 0.0, "Europe/Berlin": 1.0, "Europe/Paris": 1.0,
    "Europe/Moscow": 3.0,
    "America/New_York": -5.0, "America/Chicago": -6.0,
    "America/Denver": -7.0, "America/Los_Angeles": -8.0,
    "Australia/Sydney": 10.0,
}


def load_env_file(path: Optional[Path] = None) -> None:
    """把 .env 里的键值读进 os.environ。不覆盖已存在的系统环境变量，只执行一次。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = Path(path) if path else PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_secret(env_name: str, default: str = "") -> str:
    """按环境变量名取凭据；变量名为空则返回默认值。"""
    if not env_name:
        return default
    return os.environ.get(str(env_name).strip(), default)


# --------------------------------------------------------------------------
# 类型转换小工具：INI 里一切都是字符串
# --------------------------------------------------------------------------
def _opt(cp: configparser.ConfigParser, section: str, key: str) -> Optional[str]:
    if not cp.has_section(section) or not cp.has_option(section, key):
        return None
    value = (cp.get(section, key) or "").strip()
    return value if value != "" else None


def _as_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "是", "开"}


def _as_int(value: Optional[str], default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Optional[str], default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _env_value(name: str) -> Optional[str]:
    """读环境变量覆盖值。未设置、或只有空白，都视为「没设置」。"""
    if not name:
        return None
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _cfg(
    cp: configparser.ConfigParser,
    section: str,
    key: str,
    env_name: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """配置取值优先级：环境变量 > INI > 代码默认值。

    有了它，个人化的邮件设置都可以放进 .env（已被 .gitignore 忽略），
    仓库里的 mail.ini 就能保持通用模板，不会因为填了自己的邮箱而变脏。
    """
    override = _env_value(env_name)
    if override is not None:
        return override
    return _opt(cp, section, key) or default


def _as_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    items: List[str] = []
    for chunk in str(value).replace(";", ",").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            items.append(chunk)
    return items


# --------------------------------------------------------------------------
# 时区
# --------------------------------------------------------------------------
def resolve_timezone(name: str, logger: Any = None) -> tzinfo:
    """把时区名解析成 tzinfo。zoneinfo 不可用时退回固定偏移并给出告警。"""
    name = (name or "UTC").strip()
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+

        return ZoneInfo(name)
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        if logger is not None:
            logger.warning(
                "时区 %s 无法通过 zoneinfo 加载（%s）。"
                "建议执行 pip install tzdata；当前退化为固定偏移实现，"
                "该时区的夏令时不会被处理。",
                name, exc,
            )
    if name in _FALLBACK_OFFSETS:
        return timezone(timedelta(hours=_FALLBACK_OFFSETS[name]), name)
    if logger is not None:
        logger.warning("未知时区 %s，退化为 UTC。", name)
    return timezone.utc


# --------------------------------------------------------------------------
# 配置对象
# --------------------------------------------------------------------------
@dataclass
class ImapConfig:
    enabled: bool = True
    host: str = ""
    port: int = 993
    use_ssl: bool = True
    username: str = ""
    password: str = ""
    mailbox: str = "INBOX"
    recipient_aliases: List[str] = field(default_factory=list)
    initial_lookback_days: int = 7
    fetch_batch_size: int = 200
    timeout_seconds: int = 30
    readonly: bool = True
    webmail_url_template: str = ""
    # 只拉取最近 N 天内到达的邮件（0 = 不限制），由 [retention] 段提供
    max_age_days: int = 0
    # 增量拉取方式：uid（按上次处理到的 UID）/ since（按上次拉取的时间点）
    fetch_mode: str = "uid"
    # 归档文件夹：把识别出的通知邮件复制一份到这里（留空 = 不归档）
    archive_folder: str = ""

    @property
    def account(self) -> str:
        """游标与去重使用的账号标识。"""
        return self.username or f"{self.host}:{self.port}"

    def is_ready(self) -> bool:
        return bool(self.enabled and self.host and self.username and self.password)


@dataclass
class SmtpConfig:
    enabled: bool = True
    host: str = ""
    port: int = 465
    use_ssl: bool = True
    username: str = ""
    password: str = ""
    from_addr: str = ""
    from_name: str = "招聘邮件助手"
    max_retries: int = 3
    retry_backoff_seconds: int = 60

    @property
    def sender(self) -> str:
        return self.from_addr or self.username

    def is_ready(self) -> bool:
        return bool(self.enabled and self.host and self.username and self.password)


@dataclass
class NotifyConfig:
    poll_interval_minutes: int = 10
    daily_summary_time: str = "10:00"
    timezone: str = "Asia/Shanghai"
    recipients: List[str] = field(default_factory=list)
    cc: List[str] = field(default_factory=list)
    subject_template: str = "【招聘汇总】{date} 共 {count} 封待处理"
    max_items_per_summary: int = 50
    send_when_empty: bool = False
    categories: List[str] = field(default_factory=lambda: ["interview", "written_test"])
    include_pending_review_in_summary: bool = True
    # ---- 完成回复与每日续推 ----
    reply_completion_enabled: bool = True
    reply_allowed_senders: List[str] = field(default_factory=list)
    # 汇总之前先收一次信：用户很可能刚回复了「已完成」，
    # 不先收信就会拿着旧的未完成清单去汇总
    poll_before_summary: bool = True
    # 只在「有变化」时推送：避免一天登录几次就收到几封一样的汇总
    push_only_when_changed: bool = True
    remind_unfinished_daily: bool = True
    sort_by: str = "deadline"

    @property
    def summary_hour_minute(self):
        text = (self.daily_summary_time or "10:00").strip()
        try:
            hour_text, minute_text = text.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except (ValueError, AttributeError):
            pass
        return 10, 0


@dataclass
class ClassifierConfig:
    strategy: str = "rules_then_llm"
    rule_accept_confidence: float = 0.80
    llm_trigger_confidence: float = 0.80
    pending_review_confidence: float = 0.55
    ambiguity_margin: float = 0.10
    max_body_chars: int = 6000
    # 规则匹配范围：subject（只看主题）/ subject_body（主题 + 正文）
    rule_scope: str = "subject"
    # 主题命中关键词后直接采信的置信度
    subject_match_confidence: float = 0.95


@dataclass
class ModelConfig:
    enabled: bool = False
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    timeout_seconds: int = 60
    max_retries: int = 2
    temperature: float = 0.0
    max_tokens: int = 512
    response_format_json: bool = True
    daily_call_limit: int = 300
    fallback_to_pending_review: bool = True

    def is_ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.model and self.api_key)


@dataclass
class RetentionConfig:
    """邮件保留策略：控制「处理多久以内的邮件」与「清理多久以前的记录」。"""

    max_age_days: int = 30
    purge_after_days: int = 30
    purge_on_start: bool = True
    # 清理时保留「还没通知过 / 还没人工确认」的记录，避免把待办事项删掉
    purge_keep_unnotified: bool = True


@dataclass
class AppConfig:
    imap: ImapConfig
    smtp: SmtpConfig
    notify: NotifyConfig
    retention: RetentionConfig
    classifier: ClassifierConfig
    model: ModelConfig
    rules: Dict[str, Any]
    db_path: Path
    log_dir: Path
    project_root: Path

    def tzinfo(self, logger: Any = None) -> tzinfo:
        # RECRUIT_NOTIFY_TIMEZONE 是新名字；RECRUIT_TZ 作为旧名字继续兼容
        name = (
            os.environ.get("RECRUIT_NOTIFY_TIMEZONE")
            or os.environ.get("RECRUIT_TZ")
            or self.notify.timezone
        )
        return resolve_timezone(name, logger)


def load_rules(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取 rules.json；缺失或损坏时返回空字典，由调用方兜底。"""
    rules_path = Path(path) if path else RULES_JSON
    if not rules_path.exists():
        return {}
    try:
        return json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_config(
    mail_ini: Optional[Path] = None,
    model_ini: Optional[Path] = None,
    rules_json: Optional[Path] = None,
    logger: Any = None,
) -> AppConfig:
    """重新读盘构建配置。每次定时任务开始时调用，实现热加载。"""
    load_env_file()

    mail_path = Path(mail_ini) if mail_ini else MAIL_INI
    model_path = Path(model_ini) if model_ini else MODEL_INI

    mail_cp = configparser.ConfigParser(interpolation=None)
    mail_cp.optionxform = str  # 保留键名大小写
    mail_cp.read(mail_path, encoding="utf-8")

    model_cp = configparser.ConfigParser(interpolation=None)
    model_cp.optionxform = str
    model_cp.read(model_path, encoding="utf-8")

    retention = RetentionConfig(
        max_age_days=_as_int(_opt(mail_cp, "retention", "max_age_days"), 30),
        purge_after_days=_as_int(_opt(mail_cp, "retention", "purge_after_days"), 30),
        purge_on_start=_as_bool(_opt(mail_cp, "retention", "purge_on_start"), True),
        purge_keep_unnotified=_as_bool(
            _opt(mail_cp, "retention", "purge_keep_unnotified"), True
        ),
    )

    imap_username = (
        _env_value("RECRUIT_IMAP_USER")
        or _opt(mail_cp, "imap", "username")
        or get_secret(_opt(mail_cp, "imap", "user_env") or "")
    )
    imap = ImapConfig(
        enabled=_as_bool(_cfg(mail_cp, "imap", "enabled", "RECRUIT_IMAP_ENABLED"), True),
        host=_cfg(mail_cp, "imap", "host", "RECRUIT_IMAP_HOST") or "",
        port=_as_int(_cfg(mail_cp, "imap", "port", "RECRUIT_IMAP_PORT"), 993),
        use_ssl=_as_bool(_cfg(mail_cp, "imap", "use_ssl", "RECRUIT_IMAP_SSL"), True),
        username=imap_username,
        password=get_secret(_opt(mail_cp, "imap", "password_env") or ""),
        mailbox=_cfg(mail_cp, "imap", "mailbox", "RECRUIT_IMAP_MAILBOX") or "INBOX",
        recipient_aliases=_as_list(
            _cfg(mail_cp, "imap", "recipient_aliases", "RECRUIT_IMAP_RECIPIENT_ALIASES")
        ),
        initial_lookback_days=_as_int(
            _cfg(mail_cp, "imap", "initial_lookback_days", "RECRUIT_IMAP_LOOKBACK_DAYS"), 7
        ),
        fetch_batch_size=_as_int(_opt(mail_cp, "imap", "fetch_batch_size"), 200),
        timeout_seconds=_as_int(_opt(mail_cp, "imap", "timeout_seconds"), 30),
        readonly=_as_bool(_cfg(mail_cp, "imap", "readonly", "RECRUIT_IMAP_READONLY"), True),
        webmail_url_template=_cfg(
            mail_cp, "imap", "webmail_url_template", "RECRUIT_IMAP_WEBMAIL_URL_TEMPLATE"
        ) or "",
        max_age_days=retention.max_age_days,
        fetch_mode=(
            _cfg(mail_cp, "imap", "fetch_mode", "RECRUIT_IMAP_FETCH_MODE") or "uid"
        ).strip().lower(),
        archive_folder=(
            _cfg(mail_cp, "imap", "archive_folder", "RECRUIT_IMAP_ARCHIVE_FOLDER") or ""
        ).strip(),
    )

    smtp_username = (
        _env_value("RECRUIT_SMTP_USER")
        or _opt(mail_cp, "smtp", "username")
        or get_secret(_opt(mail_cp, "smtp", "user_env") or "")
    )
    smtp = SmtpConfig(
        enabled=_as_bool(_cfg(mail_cp, "smtp", "enabled", "RECRUIT_SMTP_ENABLED"), True),
        host=_cfg(mail_cp, "smtp", "host", "RECRUIT_SMTP_HOST") or "",
        port=_as_int(_cfg(mail_cp, "smtp", "port", "RECRUIT_SMTP_PORT"), 465),
        use_ssl=_as_bool(_cfg(mail_cp, "smtp", "use_ssl", "RECRUIT_SMTP_SSL"), True),
        username=smtp_username,
        password=get_secret(_opt(mail_cp, "smtp", "password_env") or ""),
        from_addr=_cfg(mail_cp, "smtp", "from_addr", "RECRUIT_SMTP_FROM") or "",
        from_name=_cfg(mail_cp, "smtp", "from_name", "RECRUIT_SMTP_FROM_NAME")
        or "招聘邮件助手",
        max_retries=_as_int(_opt(mail_cp, "smtp", "max_retries"), 3),
        retry_backoff_seconds=_as_int(_opt(mail_cp, "smtp", "retry_backoff_seconds"), 60),
    )

    notify = NotifyConfig(
        poll_interval_minutes=max(
            1, _as_int(_cfg(mail_cp, "notify", "poll_interval_minutes", "RECRUIT_NOTIFY_POLL_MINUTES"), 10)
        ),
        daily_summary_time=_cfg(
            mail_cp, "notify", "daily_summary_time", "RECRUIT_NOTIFY_SUMMARY_TIME"
        ) or "10:00",
        timezone=_cfg(mail_cp, "notify", "timezone", "RECRUIT_NOTIFY_TIMEZONE")
        or "Asia/Shanghai",
        recipients=_as_list(_cfg(mail_cp, "notify", "recipients", "RECRUIT_NOTIFY_RECIPIENTS")),
        cc=_as_list(_cfg(mail_cp, "notify", "cc", "RECRUIT_NOTIFY_CC")),
        subject_template=_cfg(
            mail_cp, "notify", "subject_template", "RECRUIT_NOTIFY_SUBJECT_TEMPLATE"
        ) or "【招聘汇总】{date} 共 {count} 封待处理",
        max_items_per_summary=_as_int(
            _cfg(mail_cp, "notify", "max_items_per_summary", "RECRUIT_NOTIFY_MAX_ITEMS"), 50
        ),
        send_when_empty=_as_bool(
            _cfg(mail_cp, "notify", "send_when_empty", "RECRUIT_NOTIFY_SEND_WHEN_EMPTY"), False
        ),
        categories=_as_list(
            _cfg(mail_cp, "notify", "categories", "RECRUIT_NOTIFY_CATEGORIES")
        ) or ["interview", "written_test"],
        include_pending_review_in_summary=_as_bool(
            _cfg(
                mail_cp,
                "notify",
                "include_pending_review_in_summary",
                "RECRUIT_NOTIFY_INCLUDE_PENDING",
            ),
            True,
        ),
        reply_completion_enabled=_as_bool(
            _cfg(mail_cp, "notify", "reply_completion_enabled", "RECRUIT_NOTIFY_REPLY_ENABLED"),
            True,
        ),
        reply_allowed_senders=_as_list(
            _cfg(
                mail_cp,
                "notify",
                "reply_allowed_senders",
                "RECRUIT_NOTIFY_REPLY_ALLOWED_SENDERS",
            )
        ),
        poll_before_summary=_as_bool(
            _cfg(
                mail_cp, "notify", "poll_before_summary", "RECRUIT_NOTIFY_POLL_BEFORE_SUMMARY"
            ),
            True,
        ),
        push_only_when_changed=_as_bool(
            _cfg(
                mail_cp,
                "notify",
                "push_only_when_changed",
                "RECRUIT_NOTIFY_PUSH_ONLY_WHEN_CHANGED",
            ),
            True,
        ),
        remind_unfinished_daily=_as_bool(
            _cfg(
                mail_cp,
                "notify",
                "remind_unfinished_daily",
                "RECRUIT_NOTIFY_REMIND_UNFINISHED",
            ),
            True,
        ),
        sort_by=(
            _cfg(mail_cp, "notify", "sort_by", "RECRUIT_NOTIFY_SORT_BY") or "deadline"
        ).strip().lower(),
    )

    classifier = ClassifierConfig(
        strategy=(_opt(model_cp, "classifier", "strategy") or "rules_then_llm").strip().lower(),
        rule_accept_confidence=_as_float(
            _opt(model_cp, "classifier", "rule_accept_confidence"), 0.80
        ),
        llm_trigger_confidence=_as_float(
            _opt(model_cp, "classifier", "llm_trigger_confidence"), 0.80
        ),
        pending_review_confidence=_as_float(
            _opt(model_cp, "classifier", "pending_review_confidence"), 0.55
        ),
        ambiguity_margin=_as_float(_opt(model_cp, "classifier", "ambiguity_margin"), 0.10),
        max_body_chars=_as_int(_opt(model_cp, "classifier", "max_body_chars"), 6000),
        rule_scope=(_opt(model_cp, "classifier", "rule_scope") or "subject").strip().lower(),
        subject_match_confidence=_as_float(
            _opt(model_cp, "classifier", "subject_match_confidence"), 0.95
        ),
    )

    model = ModelConfig(
        enabled=_as_bool(_opt(model_cp, "model", "enabled"), False),
        provider=_opt(model_cp, "model", "provider") or "openai_compatible",
        base_url=_opt(model_cp, "model", "base_url") or "",
        model=_opt(model_cp, "model", "model") or "",
        api_key=get_secret(_opt(model_cp, "model", "api_key_env") or ""),
        timeout_seconds=_as_int(_opt(model_cp, "model", "timeout_seconds"), 60),
        max_retries=_as_int(_opt(model_cp, "model", "max_retries"), 2),
        temperature=_as_float(_opt(model_cp, "model", "temperature"), 0.0),
        max_tokens=_as_int(_opt(model_cp, "model", "max_tokens"), 512),
        response_format_json=_as_bool(_opt(model_cp, "model", "response_format_json"), True),
        daily_call_limit=_as_int(_opt(model_cp, "model", "daily_call_limit"), 300),
        fallback_to_pending_review=_as_bool(
            _opt(model_cp, "model", "fallback_to_pending_review"), True
        ),
    )

    return AppConfig(
        imap=imap,
        smtp=smtp,
        notify=notify,
        retention=retention,
        classifier=classifier,
        model=model,
        rules=load_rules(rules_json),
        db_path=DEFAULT_DB_PATH,
        log_dir=DEFAULT_LOG_DIR,
        project_root=PROJECT_ROOT,
    )
