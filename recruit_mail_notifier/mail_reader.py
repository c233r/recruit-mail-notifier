# -*- coding: utf-8 -*-
"""IMAP 收信：增量拉取、只读访问、稳定的去重标识。

「不漏信」的三条保证
--------------------
1. 先入库、后推进游标：cursor 只在整批邮件都成功写库后才更新，
   中途崩溃下次会重放同一批（靠 message_id 去重，不会重复通知）。
2. 游标存 UIDVALIDITY：服务端 UIDVALIDITY 变了说明 UID 体系重置，
   此时自动回退到「按天数重新扫描」，而不是继续用旧 UID。
3. 不污染邮箱：取信一律用 BODY.PEEK[]（不会标已读），不删除、不移动。
   默认以只读方式打开邮箱；只有配了 archive_folder 时才会用读写方式打开
   （IMAP 的 COPY 不允许作用在只读邮箱上），此时也只是复制，原邮件不动。
"""

from __future__ import annotations

import email
import base64
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, List, Optional, Sequence, Tuple

from email.utils import parseaddr

from .config import ImapConfig
from .mime_utils import (
    decode_mime_header,
    extract_body_text,
    fallback_message_id,
    parse_mail_date,
)

_UID_RE = re.compile(rb"UID\s+(\d+)", re.IGNORECASE)
_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)", re.IGNORECASE)

# 自己拼月份缩写，避免 strftime('%b') 在中文 Windows 区域设置下输出中文月份
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class ImapError(RuntimeError):
    """IMAP 相关的可预期错误。"""


@dataclass
class RawMail:
    """一封已解码、尚未分类的邮件。"""

    uid: int
    uid_validity: Optional[int]
    message_id: str
    subject: str
    sender: str
    recipients: str
    date_header: str
    received_at: Optional[str]
    body_text: str
    mail_link: str
    mailbox: str
    account: str
    in_reply_to: str = ""
    references: str = ""
    size: int = 0
    parse_error: str = ""

    @property
    def sender_address(self) -> str:
        from email.utils import parseaddr

        return parseaddr(self.sender)[1] or self.sender


def _chunks(items: Sequence[int], size: int) -> Iterator[Sequence[int]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _imap_date(moment: datetime) -> str:
    return f"{moment.day:02d}-{_MONTHS[moment.month - 1]}-{moment.year}"


def build_search_criteria(
    last_uid: Optional[int],
    reset: bool,
    initial_lookback_days: int,
    max_age_days: int,
    now: datetime,
    last_fetch_at: Optional[datetime] = None,
    fetch_mode: str = "uid",
) -> List[str]:
    """构造 IMAP SEARCH 条件。

    ⚠️ 增量拉取必须写成 `UID 3238:*`，不能写成裸序列集 `3238:*`。
    QQ 邮箱对裸序列集会返回 `OK` 但结果为空（静默失败，不报错），
    结果就是第一次全量拉取之后再也收不到新邮件。已验证：
        SEARCH 3238:*                  -> OK, 0 封
        SEARCH UID 3238:*              -> OK, 8 封
    """
    # 按 UID 拉最精确；没有游标或显式要求 since 时，退回按时间点拉
    by_time = str(fetch_mode or "uid").lower() == "since" or reset or last_uid is None

    criteria: List[str] = []
    if by_time:
        base: Optional[datetime] = None
        if last_fetch_at is not None:
            # SINCE 只精确到天，往前多算一天，多出来的靠 message_id 去重
            base = last_fetch_at - timedelta(days=1)
        if base is None:
            base = now - timedelta(days=max(1, initial_lookback_days))
        if max_age_days > 0:
            earliest = now - timedelta(days=max_age_days)
            if base < earliest:
                base = earliest
        criteria += ["SINCE", _imap_date(base)]
    else:
        criteria += ["UID", "%d:*" % (int(last_uid or 0) + 1)]

    if max_age_days > 0:
        criteria += ["SINCE", _imap_date(now - timedelta(days=max_age_days))]
    return criteria


def encode_imap_utf7(text: str) -> str:
    """把文件夹名编成 IMAP 的 modified UTF-7（RFC 3501 §5.1.3）。

    imaplib 用 ascii 编码命令参数，中文文件夹名会直接抛 UnicodeEncodeError，
    所以 CREATE / COPY 之前必须先转码。纯 ASCII 名字原样返回。
    """
    raw_text = str(text or "")
    out: List[str] = []
    pending: List[str] = []

    def flush() -> None:
        if not pending:
            return
        raw = "".join(pending).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append("&" + encoded + "-")
        pending.clear()

    for ch in raw_text:
        code = ord(ch)
        if 0x20 <= code <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            pending.append(ch)
    flush()
    return "".join(out)


class ImapReader:
    """一个邮箱账号上的一个文件夹。用 with 语句或手动 connect/close。"""

    def __init__(self, cfg: ImapConfig, logger: Any = None, tz: Any = None) -> None:
        self.cfg = cfg
        self.logger = logger
        self.tz = tz or timezone.utc
        self._conn: Optional[imaplib.IMAP4] = None

    # ------------------------------------------------------------ 连接管理
    def __enter__(self) -> "ImapReader":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        if not self.cfg.is_ready():
            raise ImapError(
                "IMAP 配置不完整：请检查 mail.ini 的 host/username 以及 "
                "password_env 指向的环境变量是否已设置"
            )
        try:
            if self.cfg.use_ssl:
                self._conn = imaplib.IMAP4_SSL(
                    self.cfg.host, self.cfg.port, timeout=self.cfg.timeout_seconds
                )
            else:
                self._conn = imaplib.IMAP4(
                    self.cfg.host, self.cfg.port, timeout=self.cfg.timeout_seconds
                )
        except Exception as exc:
            raise ImapError(f"无法连接 IMAP 服务器 {self.cfg.host}:{self.cfg.port} -> {exc}") from exc

        # 绝不开启 imaplib 的 debug，否则账号密码会被打到日志里
        try:
            self._conn.login(self.cfg.username, self.cfg.password)
        except imaplib.IMAP4.error as exc:
            self.close()
            raise ImapError(f"IMAP 登录失败（注意 QQ/163 需要的是授权码）：{exc}") from exc

        if self.logger:
            self.logger.info("IMAP 已连接：%s@%s", self.cfg.username, self.cfg.host)

    def _select_readonly(self) -> bool:
        """是否以只读方式 select。

        归档要用 IMAP COPY，而 COPY 不允许作用在只读打开的邮箱上，
        所以配了归档文件夹时必须读写打开；取信统一用 BODY.PEEK[]，不会标已读。
        """
        if (getattr(self.cfg, "archive_folder", "") or "").strip():
            return False
        return bool(self.cfg.readonly)

    def _folder(self, name: str) -> str:
        """文件夹名 -> 可直接发给服务器的形式（modified UTF-7 + 引号）。"""
        return self._quote(encode_imap_utf7(name))

    def ensure_folder(self, folder: str) -> None:
        """确保归档文件夹存在（不存在就创建）。"""
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        typ, data = self._conn.create(self._folder(folder))
        if typ != "OK" and self.logger:
            # 最常见的原因是文件夹已经存在，不影响后面的 COPY
            self.logger.debug("创建归档文件夹 %s 返回 %s：%s", folder, typ, data)

    def copy_to_folder(self, uids: List[int], folder: str) -> int:
        """把若干封邮件复制到指定文件夹，返回复制的封数。原邮件不动。"""
        folder = (folder or "").strip()
        wanted = [int(u) for u in (uids or []) if u is not None]
        if not folder or not wanted:
            return 0
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        self.ensure_folder(folder)
        # COPY 只能在 SELECTED 状态下发（AUTH 状态下 imaplib 会直接报错），
        # 所以这里先把收件箱选回来；取信用 BODY.PEEK[]，不会标已读。
        self._conn.select(
            self._quote(self.cfg.mailbox), readonly=self._select_readonly()
        )
        copied = 0
        for start in range(0, len(wanted), 50):
            chunk = wanted[start:start + 50]
            sequence = ",".join(str(u) for u in chunk)
            typ, data = self._conn.uid("COPY", sequence, self._folder(folder))
            if typ != "OK":
                raise RuntimeError("复制到「%s」失败：%r" % (folder, data))
            copied += len(chunk)
        return copied

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            if not self.cfg.readonly:
                try:
                    conn.close()
                except Exception:
                    pass
            conn.logout()
        except Exception:
            pass

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _quote(name: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9._\-\[\]]+", name or ""):
            return name
        return '"%s"' % str(name).replace("\\", "\\\\").replace('"', '\\"')

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _uidvalidity(self) -> Optional[int]:
        assert self._conn is not None
        typ, data = self._conn.status(self._quote(self.cfg.mailbox), "(UIDVALIDITY)")
        if typ != "OK" or not data:
            return None
        first = data[0]
        blob = first if isinstance(first, (bytes, bytearray)) else str(first).encode("utf-8")
        match = _UIDVALIDITY_RE.search(blob)
        return int(match.group(1)) if match else None

    def _build_link(
        self,
        uid: int,
        message_id: str,
        subject: str,
        from_addr: str = "",
        date_text: str = "",
    ) -> str:
        """生成邮件链接。配了模板就用模板，否则退化为 mid: 标识。"""
        template = (self.cfg.webmail_url_template or "").strip()
        if template:
            try:
                return template.format(
                    account=self.cfg.account,
                    mailbox=self.cfg.mailbox,
                    uid=uid,
                    message_id=message_id.strip().strip("<>"),
                    subject=subject,
                    from_addr=from_addr,
                    date=date_text,
                )
            except (KeyError, IndexError, ValueError):
                return template
        return "mid:" + message_id.strip().strip("<>")

    # ------------------------------------------------------------ 主流程
    def fetch_new(
        self,
        last_uid: Optional[int] = None,
        uid_validity: Optional[int] = None,
        last_fetch_at: Optional[datetime] = None,
    ) -> Tuple[Optional[int], List[RawMail], int]:
        """拉取自游标之后的新邮件。

        返回 (当前 UIDVALIDITY, 邮件列表, 新的 last_uid)。
        调用方必须在邮件全部写库成功后才把新的 last_uid 存回去。
        """
        if self._conn is None:
            self.connect()
        assert self._conn is not None

        typ, data = self._conn.select(
            self._quote(self.cfg.mailbox), readonly=self._select_readonly()
        )
        if typ != "OK":
            raise ImapError(f"无法打开文件夹 {self.cfg.mailbox}：{data!r}")

        current_uidvalidity = self._uidvalidity()
        reset = last_uid is None or (
            uid_validity is not None
            and current_uidvalidity is not None
            and int(uid_validity) != int(current_uidvalidity)
        )
        if reset and last_uid is not None and self.logger:
            self.logger.warning(
                "UIDVALIDITY 由 %s 变为 %s，UID 体系已重置，改为按最近 %d 天重新扫描",
                uid_validity, current_uidvalidity, self.cfg.initial_lookback_days,
            )

        # 服务器端就把太老的邮件过滤掉：不下载、不解析，也不占带宽。
        # IMAP 的 SINCE 按「到达时间」过滤，所以结果是可靠的。
        criteria = build_search_criteria(
            last_uid=last_uid,
            reset=reset,
            initial_lookback_days=self.cfg.initial_lookback_days,
            max_age_days=self.cfg.max_age_days,
            now=self._now(),
            last_fetch_at=last_fetch_at,
            fetch_mode=self.cfg.fetch_mode,
        )
        if self.logger:
            self.logger.debug("IMAP SEARCH：%s", " ".join(criteria))

        typ, data = self._conn.uid("SEARCH", None, *criteria)
        if typ != "OK":
            raise ImapError(f"SEARCH 失败：{data!r}")

        blob = (data[0] if data else b"") or b""
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        uids = sorted(int(token) for token in blob.split())

        by_time_mode = str(self.cfg.fetch_mode or "uid").lower() == "since"
        if not reset and not by_time_mode and last_uid is not None:
            # IMAP 的 "UID n:*" 在没有新邮件时仍可能返回最后一封，必须自己过滤
            uids = [u for u in uids if u > int(last_uid)]
        if not uids and self.logger:
            # 明确记下来：以后出现「一直拉不到新邮件」时一眼能看出是搜索条件的问题
            self.logger.info("本轮没有拉到新邮件（SEARCH：%s）", " ".join(criteria))

        if self.cfg.fetch_batch_size > 0:
            skipped = len(uids) - self.cfg.fetch_batch_size
            if skipped > 0 and self.logger:
                self.logger.info(
                    "本次待处理 %d 封，按 fetch_batch_size=%d 只取最早的 %d 封，剩余下次继续",
                    len(uids), self.cfg.fetch_batch_size, self.cfg.fetch_batch_size,
                )
            uids = uids[: self.cfg.fetch_batch_size]

        mails: List[RawMail] = []
        for chunk in _chunks(uids, 20):
            mails.extend(self._fetch_chunk(list(chunk), current_uidvalidity))

        baseline = int(last_uid) if last_uid is not None else 0
        new_last_uid = max([baseline] + [m.uid for m in mails]) if mails else baseline
        if self.logger:
            self.logger.info(
                "本次拉取完成：folder=%s 新增 %d 封，游标 %s -> %s",
                self.cfg.mailbox, len(mails), last_uid, new_last_uid,
            )
        return current_uidvalidity, mails, new_last_uid

    def _fetch_chunk(self, uids: List[int], uid_validity: Optional[int]) -> List[RawMail]:
        assert self._conn is not None
        sequence = ",".join(str(u) for u in uids)
        # BODY.PEEK[] 而不是 RFC822：即使以读写方式打开也不会标成已读
        typ, data = self._conn.uid("FETCH", sequence, "(UID BODY.PEEK[])")
        if typ != "OK":
            raise ImapError(f"FETCH 失败：{data!r}")

        results: List[RawMail] = []
        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            head, payload = item[0], item[1]
            if not isinstance(payload, (bytes, bytearray)):
                continue
            head_bytes = head if isinstance(head, (bytes, bytearray)) else str(head).encode("utf-8")
            match = _UID_RE.search(head_bytes)
            if not match:
                continue
            results.append(self._parse(int(match.group(1)), bytes(payload), uid_validity))
        results.sort(key=lambda m: m.uid)
        return results

    def _parse(self, uid: int, raw: bytes, uid_validity: Optional[int]) -> RawMail:
        account = self.cfg.account
        try:
            msg = email.message_from_bytes(raw)
            subject = decode_mime_header(msg.get("Subject"))
            sender = decode_mime_header(msg.get("From"))
            recipients = ", ".join(
                part for part in (
                    decode_mime_header(msg.get("To")),
                    decode_mime_header(msg.get("Cc")),
                ) if part
            )
            message_id = (msg.get("Message-ID") or "").strip()
            date_header = msg.get("Date") or ""
            from_addr = parseaddr(sender)[1] or ""
            in_reply_to = (msg.get("In-Reply-To") or "").strip()
            references = (msg.get("References") or "").strip()
            moment = parse_mail_date(date_header)
            if moment is not None and moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            received_at = (
                moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
                if moment is not None else None
            )
            body_text = extract_body_text(msg)
        except Exception as exc:  # 单封邮件解析失败不应影响整批
            if self.logger:
                self.logger.error("UID %s 解析失败：%s", uid, exc)
            subject, sender, recipients, date_header = "", "", "", ""
            message_id, received_at, body_text = "", None, ""
            in_reply_to, references = "", ""
            if not message_id:
                message_id = fallback_message_id(account, uid_validity, uid, subject, sender)
            return RawMail(
                uid=uid, uid_validity=uid_validity, message_id=message_id,
                subject=subject, sender=sender, recipients=recipients,
                date_header=date_header, received_at=received_at, body_text=body_text,
                mail_link=self._build_link(uid, message_id, subject),
                mailbox=self.cfg.mailbox, account=account,
                in_reply_to=in_reply_to, references=references,
                size=len(raw), parse_error=str(exc),
            )

        if not message_id:
            # 没有 Message-ID 头时用稳定的伪 ID，保证多次运行得到同一个值
            message_id = fallback_message_id(account, uid_validity, uid, subject, sender)

        return RawMail(
            uid=uid, uid_validity=uid_validity, message_id=message_id,
            subject=subject, sender=sender, recipients=recipients,
            date_header=date_header, received_at=received_at, body_text=body_text,
            mail_link=self._build_link(uid, message_id, subject, from_addr, date_header),
            mailbox=self.cfg.mailbox, account=account,
            in_reply_to=in_reply_to, references=references,
            size=len(raw),
        )