# -*- coding: utf-8 -*-
"""SQLite 存储层。

要点
----
* messages 表对 message_id 建唯一索引：同一封邮件无论被 IMAP 重复投递、
  还是程序崩溃后重放，都只会入库一次 —— 这是「不重复处理」的根本保证。
* status 状态机：new -> notified / pending_review / ignored / failed
* cursors 表保存 IMAP 增量游标（UIDVALIDITY + 最大 UID），这是「不漏信」的基础。
* runs / notify_log / llm_usage 记录审计信息、发送记录与模型用量。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as _tz
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

STATUS_NEW = "new"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_IGNORED = "ignored"
STATUS_NOTIFIED = "notified"
STATUS_FAILED = "failed"
STATUS_DONE = "done"

CATEGORY_UNKNOWN = "unknown"

# 这些列是 NOT NULL：record 里没提供时用这里的默认值兜底，避免写入 NULL
_FIELD_DEFAULTS: Dict[str, Any] = {
    "account": "",
    "mailbox": "INBOX",
    "subject": "",
    "sender": "",
    "recipients": "",
    "category": CATEGORY_UNKNOWN,
    "category_label": "",
    "company": "",
    "position": "",
    "location": "",
    "mail_link": "",
    "summary": "",
    "source": "rules",
    "status": STATUS_NEW,
    "reasons": "[]",
}

_MESSAGE_FIELDS: Tuple[str, ...] = (
    "message_id", "account", "mailbox", "uid_validity", "uid",
    "subject", "sender", "recipients",
    "category", "category_label", "company", "position", "location",
    "notify_time", "event_time", "event_time_raw", "deadline", "mail_link",
    "summary", "confidence", "source", "status", "reasons", "received_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id       TEXT    NOT NULL,
    account          TEXT    NOT NULL DEFAULT '',
    mailbox          TEXT    NOT NULL DEFAULT 'INBOX',
    uid_validity     INTEGER,
    uid              INTEGER,
    subject          TEXT    NOT NULL DEFAULT '',
    sender           TEXT    NOT NULL DEFAULT '',
    recipients       TEXT    NOT NULL DEFAULT '',
    category         TEXT    NOT NULL DEFAULT 'unknown',
    category_label   TEXT    NOT NULL DEFAULT '',
    company          TEXT    NOT NULL DEFAULT '',
    position         TEXT    NOT NULL DEFAULT '',
    location         TEXT    NOT NULL DEFAULT '',
    notify_time      TEXT,
    event_time       TEXT,
    event_time_raw   TEXT,
    deadline         TEXT,
    mail_link        TEXT    NOT NULL DEFAULT '',
    summary          TEXT    NOT NULL DEFAULT '',
    confidence       REAL    NOT NULL DEFAULT 0.0,
    source           TEXT    NOT NULL DEFAULT 'rules',
    status           TEXT    NOT NULL DEFAULT 'new',
    reasons          TEXT    NOT NULL DEFAULT '',
    received_at      TEXT,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    notified_at      TEXT,
    notify_batch     TEXT,
    last_error       TEXT,
    completed_at     TEXT,
    completed_by     TEXT,
    last_notified_at TEXT,
    reminder_count   INTEGER NOT NULL DEFAULT 0,
    archived_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_message_id ON messages(message_id);
CREATE INDEX IF NOT EXISTS ix_messages_status   ON messages(status);
CREATE INDEX IF NOT EXISTS ix_messages_category ON messages(category);
CREATE INDEX IF NOT EXISTS ix_messages_received ON messages(received_at);

CREATE TABLE IF NOT EXISTS cursors (
    account      TEXT    NOT NULL,
    mailbox      TEXT    NOT NULL,
    uid_validity INTEGER,
    last_uid     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT    NOT NULL,
    PRIMARY KEY (account, mailbox)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    ok          INTEGER,
    fetched     INTEGER NOT NULL DEFAULT 0,
    inserted    INTEGER NOT NULL DEFAULT 0,
    notified    INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS notify_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT    NOT NULL,
    sent_at     TEXT    NOT NULL,
    recipients  TEXT    NOT NULL DEFAULT '',
    subject     TEXT    NOT NULL DEFAULT '',
    message_id  TEXT,
    item_count  INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS llm_usage (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# 旧库升级用：缺哪列补哪列，避免升级后报 no such column
_MIGRATIONS = {
    "messages": (
        ("position", "ALTER TABLE messages ADD COLUMN position TEXT NOT NULL DEFAULT ''"),
        ("location", "ALTER TABLE messages ADD COLUMN location TEXT NOT NULL DEFAULT ''"),
        ("deadline", "ALTER TABLE messages ADD COLUMN deadline TEXT"),
        ("completed_at", "ALTER TABLE messages ADD COLUMN completed_at TEXT"),
        ("completed_by", "ALTER TABLE messages ADD COLUMN completed_by TEXT"),
        ("last_notified_at", "ALTER TABLE messages ADD COLUMN last_notified_at TEXT"),
        ("reminder_count", "ALTER TABLE messages ADD COLUMN reminder_count INTEGER NOT NULL DEFAULT 0"),
        ("archived_at", "ALTER TABLE messages ADD COLUMN archived_at TEXT"),
    ),
    "notify_log": (
        ("message_id", "ALTER TABLE notify_log ADD COLUMN message_id TEXT"),
    ),
}


def iso_utc(moment: datetime) -> str:
    """统一的 UTC ISO8601 时间戳格式（固定带微秒）。

    保留微秒是必须的：判断「自上次推送以来有没有变化」要比较时间先后，
    秒级精度会让同一秒内发生的事情（先推送、紧接着标记完成）分不出先后，
    结果就是改了状态却不会再推送。
    """
    return moment.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def utcnow_iso() -> str:
    """当前时间的 UTC ISO8601 时间戳（固定带微秒）。"""
    return iso_utc(datetime.now(_tz.utc))


def new_batch_id() -> str:
    return datetime.now(_tz.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


class Database:
    """轻量 SQLite 封装。每次操作开新连接，避免跨线程/跨进程共享连接的问题。"""

    def __init__(self, path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ 基础
    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def session(self):
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.session() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """给已存在的旧库补上新增的列。"""
        for table, columns in _MIGRATIONS.items():
            existing = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
            for name, ddl in columns:
                if name not in existing:
                    conn.execute(ddl)

    # ---------------------------------------------------------------- 游标
    def get_cursor(self, account: str, mailbox: str) -> Optional[sqlite3.Row]:
        with self.session() as conn:
            return conn.execute(
                "SELECT * FROM cursors WHERE account = ? AND mailbox = ?",
                (account, mailbox),
            ).fetchone()

    def set_cursor(self, account: str, mailbox: str, uid_validity: Optional[int], last_uid: int) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO cursors (account, mailbox, uid_validity, last_uid, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account, mailbox) DO UPDATE SET
                    uid_validity = excluded.uid_validity,
                    last_uid     = excluded.last_uid,
                    updated_at   = excluded.updated_at
                """,
                (account, mailbox, uid_validity, int(last_uid), utcnow_iso()),
            )

    # ---------------------------------------------------------------- 邮件
    def insert_message(self, record: Dict[str, Any]) -> Tuple[bool, int]:
        """写入一封邮件。返回 (是否新插入, 行 id)。

        用 INSERT OR IGNORE 配合 message_id 唯一索引实现幂等：
        重复邮件不会报错，也不会覆盖已有记录（保留首次分类结果与已通知状态）。
        """
        now = utcnow_iso()
        values: Dict[str, Any] = {}
        for field in _MESSAGE_FIELDS:
            values[field] = record.get(field)
        for field, default in _FIELD_DEFAULTS.items():
            if values.get(field) is None:
                values[field] = default
        if values.get("confidence") is None:
            values["confidence"] = 0.0
        values["message_id"] = str(values.get("message_id") or "").strip()
        if not values["message_id"]:
            raise ValueError("message_id 不能为空，去重依赖它")
        reasons = record.get("reasons")
        values["reasons"] = (
            reasons if isinstance(reasons, str) else json.dumps(reasons or [], ensure_ascii=False)
        )
        values["created_at"] = now
        values["updated_at"] = now

        columns = ",".join(values.keys())
        placeholders = ",".join("?" for _ in values)
        with self.session() as conn:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO messages ({columns}) VALUES ({placeholders})",
                list(values.values()),
            )
            if cur.rowcount > 0:
                return True, int(cur.lastrowid or 0)
            row = conn.execute(
                "SELECT id FROM messages WHERE message_id = ?", (values["message_id"],)
            ).fetchone()
            return False, int(row["id"]) if row else -1

    def get_message_by_message_id(self, message_id: str) -> Optional[sqlite3.Row]:
        with self.session() as conn:
            return conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()

    def list_unnotified(self, categories: Optional[Sequence[str]] = None) -> List[sqlite3.Row]:
        """列出「已确认相关但还没通知过」的邮件，供每日汇总使用。"""
        sql = [
            "SELECT * FROM messages",
            "WHERE notified_at IS NULL",
            "  AND status = ?",
        ]
        params: List[Any] = [STATUS_NEW]
        if categories:
            sql.append("  AND category IN (%s)" % ",".join("?" for _ in categories))
            params.extend(categories)
        sql.append("ORDER BY COALESCE(received_at, created_at) ASC, id ASC")
        with self.session() as conn:
            return list(conn.execute(" ".join(sql), params).fetchall())

    def list_pending_review(self) -> List[sqlite3.Row]:
        with self.session() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM messages WHERE status = ? ORDER BY COALESCE(received_at, created_at) ASC",
                    (STATUS_PENDING_REVIEW,),
                ).fetchall()
            )

    def list_pending_jobs(
        self,
        categories: Optional[Sequence[str]] = None,
        include_notified: bool = True,
        sort_by: str = "deadline",
    ) -> List[sqlite3.Row]:
        """列出「还没完成」的面试/笔试，默认按截止时间由近到远排序。

        * 已完成的（completed_at 非空）永远不出现；
        * include_notified=True 时，之前提醒过但还没完成的也会继续留在列表里，
          这就是「明天继续推送未完成项」的实现。
        """
        statuses = [STATUS_NEW]
        if include_notified:
            statuses.append(STATUS_NOTIFIED)

        sql = [
            "SELECT * FROM messages",
            "WHERE completed_at IS NULL",
            "  AND status IN (%s)" % ",".join("?" for _ in statuses),
        ]
        params: List[Any] = list(statuses)
        if categories:
            sql.append("  AND category IN (%s)" % ",".join("?" for _ in categories))
            params.extend(categories)

        if sort_by == "received":
            sql.append("ORDER BY COALESCE(received_at, created_at) ASC, id ASC")
        else:
            # 没有截止时间的排在最后（(deadline IS NULL) 为 1）
            sql.append(
                "ORDER BY (deadline IS NULL), deadline ASC, "
                "COALESCE(received_at, created_at) ASC, id ASC"
            )
        with self.session() as conn:
            return list(conn.execute(" ".join(sql), params).fetchall())

    def find_jobs_by_company(
        self,
        keyword: str,
        categories: Optional[Sequence[str]] = None,
        include_completed: bool = False,
    ) -> List[sqlite3.Row]:
        """按公司名或主题模糊匹配，供 done / undo 命令使用。"""
        sql = ["SELECT * FROM messages WHERE (company LIKE ? OR subject LIKE ?)"]
        like = "%%%s%%" % str(keyword or "")
        params: List[Any] = [like, like]
        if not include_completed:
            sql.append("  AND completed_at IS NULL")
        if categories:
            sql.append("  AND category IN (%s)" % ",".join("?" for _ in categories))
            params.extend(categories)
        sql.append("ORDER BY (deadline IS NULL), deadline ASC, id ASC")
        with self.session() as conn:
            return list(conn.execute(" ".join(sql), params).fetchall())

    def mark_completed(self, ids: Iterable[int], by: str = "manual") -> int:
        """标记为已完成，之后不再出现在每日提醒里。"""
        id_list = [int(i) for i in ids]
        if not id_list:
            return 0
        now = utcnow_iso()
        placeholders = ",".join("?" for _ in id_list)
        with self.session() as conn:
            cur = conn.execute(
                f"""
                UPDATE messages
                   SET completed_at = COALESCE(completed_at, ?),
                       completed_by = ?, status = ?, updated_at = ?
                 WHERE id IN ({placeholders})
                """,
                [now, by, STATUS_DONE, now] + id_list,
            )
            return int(cur.rowcount or 0)

    def mark_undone(self, ids: Iterable[int]) -> int:
        """撤销完成标记，恢复成「待通知 / 已通知但未完成」。"""
        id_list = [int(i) for i in ids]
        if not id_list:
            return 0
        now = utcnow_iso()
        placeholders = ",".join("?" for _ in id_list)
        with self.session() as conn:
            cur = conn.execute(
                f"""
                UPDATE messages
                   SET completed_at = NULL, completed_by = NULL, updated_at = ?,
                       status = CASE WHEN notified_at IS NULL THEN ? ELSE ? END
                 WHERE id IN ({placeholders})
                """,
                [now, STATUS_NEW, STATUS_NOTIFIED] + id_list,
            )
            return int(cur.rowcount or 0)

    # 「还没处理的待办」：还没通知过，或还没人工确认
    _PURGE_ACTIONABLE = "NOT (notified_at IS NULL AND status IN ('new', 'pending_review'))"

    def purge_older_than(self, days: int, keep_unnotified: bool = True) -> Dict[str, int]:
        """删除超过保留期的旧记录（只动本地数据库，不碰邮箱）。

        keep_unnotified=True 时跳过「还没通知过 / 还没人工确认」的记录 ——
        它们是你还没处理的待办，删掉就再也提醒不到了。
        """
        empty = {"messages": 0, "protected": 0, "notify_log": 0, "runs": 0}
        if int(days or 0) <= 0:
            return empty

        cutoff = iso_utc(datetime.now(_tz.utc) - timedelta(days=int(days)))
        age_clause = "COALESCE(received_at, created_at) < ?"

        with self.session() as conn:
            protected = 0
            if keep_unnotified:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE %s AND NOT (%s)"
                    % (age_clause, self._PURGE_ACTIONABLE),
                    (cutoff,),
                ).fetchone()
                protected = int(row["c"] or 0)

            guard = (" AND " + self._PURGE_ACTIONABLE) if keep_unnotified else ""
            cur = conn.execute(
                "DELETE FROM messages WHERE %s%s" % (age_clause, guard), (cutoff,)
            )
            deleted = int(cur.rowcount or 0)
            log_cur = conn.execute("DELETE FROM notify_log WHERE sent_at < ?", (cutoff,))
            run_cur = conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))

        return {
            "messages": deleted,
            "protected": protected,
            "notify_log": int(log_cur.rowcount or 0),
            "runs": int(run_cur.rowcount or 0),
        }

    # ---------------------------------------------------------------- 运行状态
    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.session() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.session() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def has_summaryable_changes_since(self, since: str) -> bool:
        """自某个时间点以来，汇总邮件的内容会不会变（新增待通知 / 有标记完成）。"""
        with self.session() as conn:
            new_rows = conn.execute(
                "SELECT COUNT(*) AS c FROM messages "
                " WHERE status = ? AND notified_at IS NULL AND created_at > ?",
                (STATUS_NEW, since),
            ).fetchone()
            if int(new_rows["c"] or 0) > 0:
                return True
            done_rows = conn.execute(
                "SELECT COUNT(*) AS c FROM messages "
                " WHERE completed_at IS NOT NULL AND completed_at > ?",
                (since,),
            ).fetchone()
            return int(done_rows["c"] or 0) > 0

    def recent_notify_message_ids(self, limit: int = 30) -> List[str]:
        """最近几次汇总邮件的 Message-ID，用来匹配用户的回复。"""
        with self.session() as conn:
            rows = conn.execute(
                "SELECT message_id FROM notify_log "
                "WHERE message_id IS NOT NULL AND message_id != '' "
                "ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [row["message_id"] for row in rows]

    def list_unarchived_uids(self, categories: List[str], mailbox: str = "") -> List[int]:
        """还没归档过、且属于指定类别的邮件 UID（用于一次性补齐归档）。"""
        wanted = [str(c) for c in (categories or []) if str(c).strip()]
        if not wanted:
            return []
        sql = (
            "SELECT DISTINCT uid FROM messages "
            " WHERE uid IS NOT NULL AND archived_at IS NULL "
            "   AND status != ? AND category IN (%s)" % ",".join("?" * len(wanted))
        )
        params: List[Any] = ["ignored"] + wanted
        if mailbox:
            sql += " AND mailbox = ?"
            params.append(mailbox)
        with self.session() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [int(row["uid"]) for row in rows]

    def mark_archived(self, uids: Iterable[int]) -> int:
        """把这些 UID 标记为已归档，避免重复复制到归档文件夹。"""
        wanted = [int(u) for u in (uids or []) if u is not None]
        if not wanted:
            return 0
        now = utcnow_iso()
        total = 0
        with self.session() as conn:
            for start in range(0, len(wanted), 200):
                chunk = wanted[start:start + 200]
                marks = ",".join("?" * len(chunk))
                cursor = conn.execute(
                    "UPDATE messages SET archived_at = ? WHERE uid IN (%s)" % marks,
                    tuple([now] + chunk),
                )
                total += cursor.rowcount or 0
        return total


    def mark_notified(self, ids: Iterable[int], batch_id: str) -> int:
        """发送成功后更新通知状态。

        未完成的记录会被反复提醒，所以这里不是「只标一次」：
        首次设置 notified_at，之后每次只累加 reminder_count 和 last_notified_at。
        """
        id_list = [int(i) for i in ids]
        if not id_list:
            return 0
        now = utcnow_iso()
        placeholders = ",".join("?" for _ in id_list)
        with self.session() as conn:
            cur = conn.execute(
                f"""
                UPDATE messages
                   SET status = CASE WHEN completed_at IS NULL THEN ? ELSE status END,
                       notified_at = COALESCE(notified_at, ?),
                       last_notified_at = ?,
                       reminder_count = reminder_count + 1,
                       notify_batch = ?,
                       updated_at = ?,
                       last_error = NULL
                 WHERE id IN ({placeholders})
                """,
                [STATUS_NOTIFIED, now, now, batch_id, now] + id_list,
            )
            return int(cur.rowcount or 0)

    def set_status(self, message_id: str, status: str, error: Optional[str] = None) -> None:
        with self.session() as conn:
            conn.execute(
                "UPDATE messages SET status = ?, last_error = ?, updated_at = ? WHERE message_id = ?",
                (status, error, utcnow_iso(), message_id),
            )

    def set_status_by_id(self, row_id: int, status: str, error: Optional[str] = None) -> None:
        with self.session() as conn:
            conn.execute(
                "UPDATE messages SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, error, utcnow_iso(), int(row_id)),
            )

    # ---------------------------------------------------------------- 运行审计
    def start_run(self, kind: str) -> int:
        with self.session() as conn:
            cur = conn.execute(
                "INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, utcnow_iso())
            )
            return int(cur.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        ok: bool,
        fetched: int = 0,
        inserted: int = 0,
        notified: int = 0,
        errors: int = 0,
        note: str = "",
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET finished_at = ?, ok = ?, fetched = ?, inserted = ?,
                       notified = ?, errors = ?, note = ?
                 WHERE id = ?
                """,
                (utcnow_iso(), 1 if ok else 0, fetched, inserted, notified, errors, note, int(run_id)),
            )

    def record_notify(
        self,
        batch_id: str,
        recipients: Sequence[str],
        subject: str,
        item_count: int,
        ok: bool,
        error: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> None:
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO notify_log
                    (batch_id, sent_at, recipients, subject, message_id, item_count, ok, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    utcnow_iso(),
                    ",".join(recipients or []),
                    subject,
                    message_id,
                    int(item_count),
                    1 if ok else 0,
                    error,
                ),
            )

    # ---------------------------------------------------------------- 模型用量
    def llm_calls_today(self, day: Optional[str] = None) -> int:
        day = day or datetime.now(_tz.utc).strftime("%Y-%m-%d")
        with self.session() as conn:
            row = conn.execute("SELECT calls FROM llm_usage WHERE day = ?", (day,)).fetchone()
            return int(row["calls"]) if row else 0

    def bump_llm_calls(self, day: Optional[str] = None, amount: int = 1) -> int:
        day = day or datetime.now(_tz.utc).strftime("%Y-%m-%d")
        with self.session() as conn:
            conn.execute(
                """
                INSERT INTO llm_usage (day, calls) VALUES (?, ?)
                ON CONFLICT(day) DO UPDATE SET calls = calls + excluded.calls
                """,
                (day, int(amount)),
            )
            row = conn.execute("SELECT calls FROM llm_usage WHERE day = ?", (day,)).fetchone()
            return int(row["calls"]) if row else int(amount)

    # ---------------------------------------------------------------- 统计
    def stats(self) -> Dict[str, Any]:
        with self.session() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            by_status = {
                row["status"]: row["c"]
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS c FROM messages GROUP BY status"
                ).fetchall()
            }
            by_category = {
                row["category"]: row["c"]
                for row in conn.execute(
                    "SELECT category, COUNT(*) AS c FROM messages GROUP BY category"
                ).fetchall()
            }
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE notified_at IS NULL AND status = ?",
                (STATUS_NEW,),
            ).fetchone()["c"]
        return {
            "total": int(total),
            "by_status": by_status,
            "by_category": by_category,
            "awaiting_notify": int(pending),
        }