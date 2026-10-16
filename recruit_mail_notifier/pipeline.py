# -*- coding: utf-8 -*-
"""主流程编排：采集 -> 分类 -> 入库 -> 汇总 -> 发送 -> 标记已通知。

不漏信 / 不重复的两条关键规则
-----------------------------
1. 游标只在整批邮件都成功写库后才推进；中途失败就停在失败那封之前，
   下一轮从同一位置重放（靠 message_id 唯一索引保证不会重复通知）。
2. 只有发送成功才调用 mark_notified；发送失败保持 new 状态，下一轮自动重试。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .classifier import LlmBudget, classify_mail
from .config import load_config
from . import replies
from .db import (
    Database,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_IGNORED,
    STATUS_NEW,
    new_batch_id,
    utcnow_iso,
)
from .llm_client import LlmClient
from .mail_reader import ImapReader, RawMail
from .notifier import FileNotifier, NotifyError, SmtpNotifier, build_summary_content


class Pipeline:
    """把各模块串起来。每轮任务开始前都会重新加载配置，实现热加载。"""

    def __init__(
        self,
        config: Any = None,
        logger: Any = None,
        reader_factory: Optional[Callable[[Any], Any]] = None,
        notifier: Any = None,
        dry_run: bool = False,
    ) -> None:
        self.logger = logger
        self._explicit_config = config is not None
        self.config_factory: Optional[Callable[[], Any]] = None
        self.cfg = config or load_config(logger=logger)
        self.db = Database(self.cfg.db_path)
        self.reader_factory = reader_factory
        self.notifier = notifier
        self.dry_run = dry_run

    # ------------------------------------------------------------ 基础设施
    def reload_config(self) -> Any:
        """重新读两个 INI 与 rules.json，实现「改完配置保存即生效」。

        显式传入 config 的调用方（本地测试、被别的程序嵌入）保持自己管理配置，
        不会被这里的默认路径覆盖。
        """
        if self.config_factory is not None:
            self.cfg = self.config_factory()
        elif self._explicit_config:
            return self.cfg
        else:
            self.cfg = load_config(logger=self.logger)
        self.db = Database(self.cfg.db_path)
        return self.cfg

    def init_db(self) -> None:
        self.db.init_schema()

    def _make_reader(self) -> Any:
        if self.reader_factory is not None:
            return self.reader_factory(self.cfg)
        return ImapReader(self.cfg.imap, logger=self.logger, tz=self.cfg.tzinfo(self.logger))

    def _make_llm_client(self) -> Optional[LlmClient]:
        if not self.cfg.model.is_ready():
            scope = str(getattr(self.cfg.classifier, "rule_scope", "subject")).lower()
            if scope == "subject" and self.logger:
                self.logger.warning(
                    "规则只看主题（rule_scope=subject），但大模型未就绪："
                    "主题里没写「笔试/测评/面试」的邮件不会被推送，只会进「待确认」。"
                    "要启用模型判断，请在 config/model.ini 把 [model] enabled 改成 true，"
                    "并在 .env 里设好 RECRUIT_LLM_API_KEY。"
                )
            return None
        categories = list((self.cfg.rules.get("categories") or {}).keys())
        for extra in ("other", "unknown"):
            if extra not in categories:
                categories.append(extra)
        return LlmClient(self.cfg.model, self.cfg.classifier, categories, self.logger)

    def _make_notifier(self) -> Any:
        if self.notifier is not None:
            return self.notifier
        if self.dry_run:
            return FileNotifier(Path(self.cfg.log_dir) / "outbox", self.logger)
        return SmtpNotifier(self.cfg.smtp, self.logger)

    # ------------------------------------------------------------ 入库
    def store_mail(self, mail: RawMail, llm_client: Any = None, budget: Any = None) -> Tuple[bool, bool]:
        """分类并写入一封邮件。返回 (是否成功, 是否是新插入)。"""
        verdict = self.classify_incoming_mail(mail)

        # 汇总邮件的收件人就是自己，下一轮拉取会把它自己再收回来，必须排除
        if verdict == replies.OWN_SUMMARY:
            if self.logger:
                self.logger.info(
                    "跳过系统自己发出的汇总邮件（不当作完成回复）：%s",
                    (mail.subject or "")[:60],
                )
            return True, False

        # 「完成回复」必须优先识别，否则会被当成普通招聘邮件去分类
        if verdict == replies.COMPLETION_REPLY and self.reply_sender_allowed(mail):
            return self.store_completion_reply(mail)

        classification = classify_mail(
            mail, self.cfg, llm_client=llm_client, budget=budget, logger=self.logger
        )
        record: Dict[str, Any] = {
            "message_id": mail.message_id,
            "account": mail.account,
            "mailbox": mail.mailbox,
            "uid_validity": mail.uid_validity,
            "uid": mail.uid,
            "subject": mail.subject,
            "sender": mail.sender,
            "recipients": mail.recipients,
            "mail_link": mail.mail_link,
            "received_at": mail.received_at,
        }
        record.update(classification.to_record())
        inserted, row_id = self.db.insert_message(record)

        if inserted and self.logger:
            self.logger.info(
                "入库 [%s] %s | %s | 置信度 %.2f (%s) | 状态=%s",
                classification.label or classification.category,
                classification.company or "未识别公司",
                (mail.subject or "")[:60],
                classification.confidence,
                classification.source,
                classification.status,
            )
        elif not inserted and self.logger:
            self.logger.debug("重复邮件（message_id 已存在），跳过：%s", mail.message_id)
        if mail.parse_error and self.logger:
            self.logger.warning("邮件 %s 解析时出现异常：%s", mail.message_id, mail.parse_error)
        return True, inserted

    # ------------------------------------------------------------ 完成回复
    def classify_incoming_mail(self, mail: RawMail) -> str:
        """把收到的邮件分成：系统自己的汇总 / 用户的完成回复 / 其他。"""
        display = parseaddr(getattr(mail, "sender", "") or "")[0] or ""
        return replies.classify_incoming(
            getattr(mail, "subject", "") or "",
            getattr(mail, "in_reply_to", "") or "",
            getattr(mail, "references", "") or "",
            getattr(mail, "body_text", "") or "",
            display,
            getattr(self.cfg.smtp, "from_name", "") or "",
        )

    def reply_sender_allowed(self, mail: RawMail) -> bool:
        """完成回复必须是白名单里的人发来的，防止别人的邮件误触发。"""
        allowed = {
            str(a).strip().lower()
            for a in (self.cfg.notify.reply_allowed_senders or [])
            if str(a).strip()
        }
        if not allowed:
            allowed = {
                str(r).strip().lower()
                for r in (self.cfg.notify.recipients or [])
                if str(r).strip()
            }
        sender = str(getattr(mail, "sender_address", "") or "").strip().lower()
        if allowed and sender not in allowed:
            if self.logger:
                self.logger.debug("发件人 %s 不在完成回复白名单里，按普通邮件处理", sender)
            return False
        return True

    def is_completion_reply(self, mail: RawMail) -> bool:
        """判断这是不是「回复汇总邮件、告诉我哪些公司已经完成」。"""
        if not self.cfg.notify.reply_completion_enabled:
            return False
        return (
            self.classify_incoming_mail(mail) == replies.COMPLETION_REPLY
            and self.reply_sender_allowed(mail)
        )

    def store_completion_reply(self, mail: RawMail) -> Tuple[bool, bool]:
        parsed = replies.parse_completion_body(mail.body_text, self.cfg.rules)
        pending = self.db.list_pending_jobs(self.cfg.notify.categories)

        # 安全阀：正文很长、又没有「完成」字眼、也没有明确编号，
        # 很可能是引用没切干净，这种情况宁可不动任何记录，也不要误标。
        suspicious = (
            (not parsed.has_completion_word)
            and (not parsed.ids)
            and len(parsed.body) > 300
        )

        matched = []
        if not suspicious:
            if parsed.mark_all:
                matched = list(pending)
            else:
                matched = replies.match_pending_companies(parsed, pending, self.cfg.rules)
        matched_ids = [row["id"] for row in matched]

        record = {
            "message_id": mail.message_id,
            "account": mail.account,
            "mailbox": mail.mailbox,
            "uid_validity": mail.uid_validity,
            "uid": mail.uid,
            "subject": mail.subject,
            "sender": mail.sender,
            "recipients": mail.recipients,
            "mail_link": mail.mail_link,
            "received_at": mail.received_at,
            "category": "reply",
            "category_label": "完成回复",
            "company": "",
            "notify_time": mail.received_at,
            "summary": parsed.body[:180],
            "confidence": 1.0,
            "source": "reply",
            "status": STATUS_IGNORED,
            "reasons": [
                "识别为汇总邮件的完成回复",
                "全部完成指令" if parsed.mark_all
                else (
                    "指定编号：%s" % "、".join("#%d" % i for i in parsed.ids)
                    if parsed.ids
                    else "提到：%s" % "、".join(parsed.mentions[:10])
                ),
                "匹配到 %d 条未完成记录" % len(matched_ids),
            ],
        }
        inserted, _row_id = self.db.insert_message(record)
        if not inserted:
            if self.logger:
                self.logger.debug("完成回复已处理过，跳过：%s", mail.message_id)
            return True, False

        if matched_ids:
            self.db.mark_completed(matched_ids, by="reply")

        if self.logger:
            names = [str(row["company"] or row["subject"]) for row in matched]
            if suspicious:
                self.logger.warning(
                    "完成回复正文过长且没有「完成」字眼，为安全起见未标记任何记录：%s",
                    parsed.body[:120],
                )
            else:
                self.logger.info(
                    "收到完成回复：标记完成 %d 条 -> %s",
                    len(matched_ids),
                    "、".join(names) if names else "（没有匹配到未完成记录）",
                )
                if parsed.ids:
                    missing = sorted(
                        set(int(i) for i in parsed.ids)
                        - set(int(i) for i in matched_ids)
                    )
                    if missing:
                        self.logger.warning(
                            "回复里指定的编号不在未完成列表里（可能已完成或被清理）：%s",
                            "、".join("#%d" % i for i in missing),
                        )
                elif parsed.mentions and not matched_ids and not parsed.mark_all:
                    self.logger.warning(
                        "完成回复提到的公司都没匹配上未完成记录：%s",
                        "、".join(parsed.mentions[:10]),
                    )
        return True, True

    def ingest_mails(self, mails: Sequence[RawMail]) -> Dict[str, int]:
        """把一批邮件分类入库（不涉及 IMAP，本地测试与导入 .eml 都走这里）。"""
        llm_client = self._make_llm_client()
        budget = LlmBudget(self.db, self.cfg.model.daily_call_limit, self.logger)
        inserted = errors = 0
        by_status: Dict[str, int] = {}
        for mail in sorted(mails, key=lambda m: m.uid or 0):
            try:
                _, was_new = self.store_mail(mail, llm_client, budget)
                if was_new:
                    inserted += 1
                row = self.db.get_message_by_message_id(mail.message_id)
                status = row["status"] if row else "unknown"
                by_status[status] = by_status.get(status, 0) + 1
            except Exception as exc:
                errors += 1
                if self.logger:
                    self.logger.exception("邮件 %s 入库失败：%s", mail.message_id, exc)
        return {"inserted": inserted, "errors": errors, "by_status": by_status}

    def _should_archive(self, message_id: str) -> bool:
        """这条记录是否属于「该进归档文件夹」的招聘通知。"""
        row = self.db.get_message_by_message_id(message_id)
        if row is None:
            return False
        if str(row["category"] or "") not in set(self.cfg.notify.categories or []):
            return False
        return str(row["status"] or "") != "ignored"

    # ------------------------------------------------------------ 轮询
    def run_poll(self) -> Dict[str, Any]:
        """拉取新邮件并分类入库。"""
        self.reload_config()
        run_id = self.db.start_run("poll")
        fetched = inserted = errors = 0
        skipped_old = 0
        note = ""
        ok = True
        try:
            if not self.cfg.imap.is_ready():
                raise RuntimeError(
                    "IMAP 未配置完整：请检查 mail.ini 的 imap.host/username，"
                    "以及 password_env 指向的环境变量是否已设置"
                )

            account = self.cfg.imap.account
            mailbox = self.cfg.imap.mailbox
            cursor = self.db.get_cursor(account, mailbox)
            last_uid = int(cursor["last_uid"]) if cursor else None
            uid_validity = (
                int(cursor["uid_validity"])
                if cursor is not None and cursor["uid_validity"] is not None
                else None
            )

            # 「上一次拉取的时间点」：没有游标（比如刚删过数据库）时按它往后捞
            last_fetch_raw = self.db.get_meta("last_fetch_at")
            last_fetch_at = None
            if last_fetch_raw:
                try:
                    last_fetch_at = datetime.fromisoformat(
                        str(last_fetch_raw).replace("Z", "+00:00")
                    )
                except ValueError:
                    last_fetch_at = None

            reader = self._make_reader()
            try:
                current_uid_validity, mails, candidate_last_uid = reader.fetch_new(
                    last_uid, uid_validity, last_fetch_at
                )
            finally:
                closer = getattr(reader, "close", None)
                if callable(closer):
                    closer()

            fetched = len(mails)
            llm_client = self._make_llm_client()
            budget = LlmBudget(self.db, self.cfg.model.daily_call_limit, self.logger)

            # 只有连续成功处理到的 UID 才推进游标，保证不漏信
            baseline = last_uid or 0
            advance_to = baseline
            ordered = sorted(mails, key=lambda m: m.uid or 0)
            failed_uid: Optional[int] = None
            archive_uids: List[int] = []
            for mail in ordered:
                try:
                    if self._is_too_old(mail):
                        # 超过保留期：不解析、不入库，但游标照常推进，
                        # 否则每一轮都会把它重新拉一遍
                        skipped_old += 1
                        if self.logger:
                            self.logger.debug("跳过超期邮件：%s", (mail.subject or "")[:60])
                        advance_to = max(advance_to, mail.uid or 0)
                        continue
                    _, was_new = self.store_mail(mail, llm_client, budget)
                    if was_new:
                        inserted += 1
                        if self._should_archive(mail.message_id):
                            archive_uids.append(int(mail.uid or 0))
                    advance_to = max(advance_to, mail.uid or 0)
                except Exception as exc:
                    errors += 1
                    failed_uid = mail.uid
                    ok = False
                    note = "UID %s 处理失败，游标停在它之前，下一轮重试" % mail.uid
                    if self.logger:
                        self.logger.exception("UID %s 处理失败：%s", mail.uid, exc)
                    break

            # 归档：把识别出的通知「复制」到专用文件夹（mail.ini 配了才做）。
            # 失败不影响收信结果 —— 邮件已经入库，不能因为归档失败就卡住游标。
            archive_folder = (getattr(self.cfg.imap, "archive_folder", "") or "").strip()
            if archive_folder and archive_uids:
                try:
                    archiver = self._make_reader()
                    try:
                        copied = archiver.copy_to_folder(archive_uids, archive_folder)
                        if self.logger:
                            self.logger.info(
                                "已复制 %d 封到归档文件夹「%s」", copied, archive_folder
                            )
                    finally:
                        closer = getattr(archiver, "close", None)
                        if callable(closer):
                            closer()
                except Exception as exc:
                    if self.logger:
                        self.logger.error(
                            "归档到「%s」失败（不影响收信）：%s", archive_folder, exc
                        )

            if errors == 0:
                advance_to = max(advance_to, candidate_last_uid or 0)
            self.db.set_cursor(account, mailbox, current_uid_validity, advance_to)
            if errors == 0:
                # 记下「这一次拉到哪儿了」，下次运行接着往下捞
                self.db.set_meta("last_fetch_at", utcnow_iso())

            if self.logger:
                self.logger.info(
                    "轮询完成：拉取 %d 封，写入 %d 封，跳过超期 %d 封，"
                    "处理至 UID %s（%s），错误 %d 个",
                    fetched, inserted, skipped_old, advance_to,
                    "全部成功" if errors == 0 else "部分失败", errors,
                )
        except Exception as exc:
            ok = False
            errors += 1
            note = str(exc)
            if self.logger:
                self.logger.exception("轮询失败：%s", exc)
        finally:
            self.db.finish_run(run_id, ok, fetched, inserted, 0, errors, note)

        return {
            "ok": ok, "fetched": fetched, "inserted": inserted,
            "skipped_old": skipped_old, "errors": errors, "note": note,
        }

    # ------------------------------------------------------------ 保留期
    def _is_too_old(self, mail: RawMail) -> bool:
        """邮件自身时间超过保留期则返回 True（超过就不处理）。"""
        days = int(getattr(self.cfg.retention, "max_age_days", 0) or 0)
        if days <= 0 or not getattr(mail, "received_at", None):
            return False
        try:
            moment = datetime.fromisoformat(str(mail.received_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment < datetime.now(timezone.utc) - timedelta(days=days)

    def run_purge(self, days: Optional[int] = None, force: bool = False) -> Dict[str, Any]:
        """删除数据库里超过保留期的旧记录。邮箱里的邮件不受影响。

        force=True 时连「还没通知过」的待办也一起删。
        """
        self.reload_config()
        keep_days = int(
            days if days is not None
            else getattr(self.cfg.retention, "purge_after_days", 0) or 0
        )
        keep_unnotified = bool(
            getattr(self.cfg.retention, "purge_keep_unnotified", True)
        ) and not force
        run_id = self.db.start_run("purge")
        ok = True
        note = ""
        try:
            if keep_days <= 0:
                note = "purge_after_days=0，未启用清理"
                if self.logger:
                    self.logger.info(note)
                return {"ok": True, "skipped": True, "note": note}

            stats = self.db.purge_older_than(keep_days, keep_unnotified=keep_unnotified)
            note = "messages=%d notify_log=%d runs=%d" % (
                stats["messages"], stats["notify_log"], stats["runs"],
            )
            if self.logger:
                self.logger.info(
                    "保留期清理完成（保留最近 %d 天%s）：%s",
                    keep_days,
                    "，跳过未处理的待办" if keep_unnotified else "，含未处理的待办",
                    note,
                )
                if stats.get("protected"):
                    self.logger.info(
                        "另有 %d 封是「还没通知过 / 还没人工确认」的待办，本次保留未删"
                        "（想一起删：purge_keep_unnotified=false，或 main.py purge --force）",
                        stats["protected"],
                    )
            return {
                "ok": True, "days": keep_days, "keep_unnotified": keep_unnotified,
                **stats, "note": note,
            }
        except Exception as exc:
            ok = False
            note = str(exc)
            if self.logger:
                self.logger.exception("保留期清理失败：%s", exc)
            return {"ok": False, "note": note}
        finally:
            self.db.finish_run(run_id, ok, 0, 0, 0, 0, note)

    # ------------------------------------------------------------ 一次执行
    def run_once(self) -> Dict[str, Any]:
        """跑一次就结束：收信 -> 分类入库 -> 汇总推送。

        这就是「开机自启」跑的那条路径 —— 不常驻、不定时，
        每次运行把上次拉取之后的邮件补上，同步数据库并推送，然后退出。
        """
        started = utcnow_iso()
        poll_result = self.run_poll()
        # 刚刚已经收过信了，汇总不必再收一次
        summary_result = self.run_summary(poll_first=False)
        self.db.set_meta("last_run_at", started)
        if self.logger:
            self.logger.info(
                "本次运行结束：拉取 %s 封 / 新增 %s 封 / 推送 %s 封",
                poll_result.get("fetched"), poll_result.get("inserted"),
                summary_result.get("notified"),
            )
        return {"poll": poll_result, "summary": summary_result}

    # ------------------------------------------------------------ 汇总发送
    def run_summary(
        self,
        force: bool = False,
        now: Optional[datetime] = None,
        poll_first: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """汇总尚未通知的相关邮件并发送，成功后标记为已通知。

        默认会先收一次信 —— 你很可能刚回复了「某某公司 已完成」，
        不先收信就会拿着旧的未完成清单去汇总（这是之前回复不生效的直接原因）。
        """
        self.reload_config()

        if poll_first is None:
            poll_first = bool(getattr(self.cfg.notify, "poll_before_summary", True))
        if poll_first:
            poll_result = self.run_poll()
            if self.logger:
                self.logger.info(
                    "汇总前先收信：拉取 %s 封，写入 %s 封，跳过超期 %s 封",
                    poll_result.get("fetched"), poll_result.get("inserted"),
                    poll_result.get("skipped_old"),
                )
                if not poll_result.get("ok"):
                    self.logger.warning(
                        "汇总前的收信没成功（%s），本次仍用现有数据汇总",
                        poll_result.get("note"),
                    )

        # 只在「有变化」时推送：一天登录几次不该收到几封一样的汇总
        if not force and bool(getattr(self.cfg.notify, "push_only_when_changed", True)):
            last_push = self.db.get_meta("last_push_at")
            if last_push and not self.db.has_summaryable_changes_since(last_push):
                note = "自上次推送以来没有变化（没有新的待通知邮件，也没有标记完成），跳过推送"
                if self.logger:
                    self.logger.info(note)
                return {"ok": True, "skipped": True, "notified": 0, "note": note}

        tz = self.cfg.tzinfo(self.logger)
        now = now or datetime.now(tz)
        run_id = self.db.start_run("summary")
        notified = errors = 0
        ok = True
        note = ""
        subject = ""

        try:
            # 未完成的都要继续推送：包含之前提醒过但还没做完的（每日续推）
            rows = self.db.list_pending_jobs(
                self.cfg.notify.categories,
                include_notified=self.cfg.notify.remind_unfinished_daily,
                sort_by=self.cfg.notify.sort_by,
            )
            pending_rows = (
                self.db.list_pending_review()
                if self.cfg.notify.include_pending_review_in_summary
                else []
            )

            # 「待确认」条目只是附在汇总邮件末尾的提醒，不单独触发发送，
            # 否则每天都会收到一封只有待确认清单的邮件。
            if not rows and not self.cfg.notify.send_when_empty:
                note = "没有待通知的相关邮件，跳过本次发送"
                if pending_rows:
                    note += "（另有 %d 封待确认，可用 pending 命令查看）" % len(pending_rows)
                if self.logger:
                    self.logger.info(note)
                return {"ok": True, "notified": 0, "skipped": True, "note": note}

            subject, text_body, html_body = build_summary_content(
                rows, self.cfg, tz, now=now, pending_rows=pending_rows
            )
            batch_id = new_batch_id()
            notifier = self._make_notifier()
            sent_message_id = ""

            try:
                sent_message_id = notifier.send(
                    subject, text_body, html_body,
                    recipients=self.cfg.notify.recipients, cc=self.cfg.notify.cc,
                )
            except Exception as exc:
                errors += 1
                ok = False
                note = "发送失败：%s" % exc
                self.db.record_notify(
                    batch_id, self.cfg.notify.recipients, subject, len(rows), False, str(exc)
                )
                # 保持 new 状态，下一轮重新尝试，绝不误标为已通知
                for row in rows:
                    self.db.set_status_by_id(row["id"], STATUS_NEW, str(exc))
                if self.logger:
                    self.logger.exception("汇总邮件发送失败：%s", exc)
                return {"ok": False, "notified": 0, "errors": errors, "note": note}

            self.db.record_notify(
                batch_id, self.cfg.notify.recipients, subject, len(rows), True, None,
                message_id=sent_message_id,
            )
            notified = self.db.mark_notified([row["id"] for row in rows], batch_id)
            self.db.set_meta("last_push_at", utcnow_iso())
            if self.logger:
                self.logger.info(
                    "汇总已发送：主题=%s，标记已通知 %d 封，附带待确认 %d 封",
                    subject, notified, len(pending_rows),
                )
        except Exception as exc:
            ok = False
            errors += 1
            note = str(exc)
            if self.logger:
                self.logger.exception("汇总任务失败：%s", exc)
        finally:
            self.db.finish_run(run_id, ok, 0, 0, notified, errors, note)

        return {"ok": ok, "notified": notified, "errors": errors, "note": note, "subject": subject}