# -*- coding: utf-8 -*-
"""命令行入口。

常用命令
--------
  python main.py check                 检查配置（不联网）
  python main.py init-db               初始化数据库
  python main.py classify-eml a.eml    离线分析本地 .eml（不连邮箱，验证分类规则）
  python main.py poll                  立刻收一次信并分类
  python main.py summary --dry-run     立刻汇总一次，只写文件不真的发信
  python main.py summary               立刻真的发送汇总
  python main.py pending               查看「待确认」的低置信度邮件
  python main.py list --status new     查看已入库邮件
  python main.py run                   常驻运行：定时轮询 + 每日汇总
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .classifier import RuleClassifier, classify_mail
from .config import load_config
from .db import Database
from .logging_setup import get_logger, setup_logging
from .mail_reader import ImapReader
from .notifier import format_deadline
from .pipeline import Pipeline
from .scheduler import Scheduler


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recruit-mail-notifier",
        description="招聘邮件通知系统：定时读邮箱、识别面试/笔试通知、每日汇总提醒。",
    )
    parser.add_argument("--dry-run", action="store_true", help="不真的发信，汇总写到 logs/outbox/")
    parser.add_argument("--log-level", default="INFO", help="日志级别，默认 INFO")
    sub = parser.add_subparsers(dest="command")

    # 让 --dry-run 也能写在子命令后面（main.py summary --dry-run），更符合直觉。
    # default=SUPPRESS 的作用：子命令没写这个参数时不去覆盖顶层已解析的值，
    # 所以 `main.py --dry-run summary` 和 `main.py summary --dry-run` 都能用。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dry-run", action="store_true", default=argparse.SUPPRESS,
        help="不真的发信，汇总写到 logs/outbox/",
    )

    sub.add_parser("init-db", help="初始化数据库表")
    sub.add_parser("check", help="检查配置是否就绪（不联网）")
    sub.add_parser("poll", help="立刻执行一次收信 + 分类", parents=[common])
    sub.add_parser("stats", help="统计概览")
    p_archive = sub.add_parser(
        "archive",
        help="把已入库的招聘通知一次性复制到 mail.ini 配的归档文件夹",
    )
    p_archive.set_defaults(func=_cmd_archive)

    p_purge = sub.add_parser("purge", help="删除数据库里超过保留期的旧记录")
    p_purge.add_argument("--days", type=int, default=None, help="覆盖 mail.ini 的 purge_after_days")
    p_purge.add_argument("--force", action="store_true", help="连「还没通知过」的待办也一起删")
    sub.add_parser("pending", help="列出待确认的低置信度邮件").add_argument(
        "--limit", type=int, default=50
    )

    p_replies = sub.add_parser("replies", help="查看处理过的完成回复（什么时候标记了什么）")
    p_replies.add_argument("--limit", type=int, default=20)

    p_summary = sub.add_parser("summary", help="立刻执行一次汇总发送", parents=[common])
    p_summary.add_argument("--force", action="store_true", help="即使没有新邮件也发一封")
    p_summary.add_argument(
        "--no-poll-first", action="store_true",
        help="汇总前不先收信（默认会先收一次，好拿到你刚发的回复）",
    )

    p_run = sub.add_parser(
        "run",
        help="一次执行：收信 + 分类 + 汇总推送，然后退出（开机自启跑的就是这个）",
        parents=[common],
    )
    p_run.add_argument(
        "--watch", action="store_true",
        help="改成常驻模式：每 N 分钟轮询 + 每天定时汇总（一般不需要）",
    )
    p_run.add_argument(
        "--no-poll-on-start", action="store_true", help="（仅 --watch）启动时不立刻轮询一次"
    )
    p_run.add_argument(
        "--summary-on-start", action="store_true", help="（仅 --watch）启动时立刻汇总一次"
    )

    p_eml = sub.add_parser("classify-eml", help="离线分析本地 .eml 文件，验证分类规则")
    p_eml.add_argument("paths", nargs="+", help="一个或多个 .eml 文件路径")
    p_eml.add_argument("--no-store", action="store_true", help="只看结果，不写入数据库")

    p_done = sub.add_parser("done", help="把公司标记为已完成（明天不再提醒）")
    p_done.add_argument("companies", nargs="*", help="公司名，可以写多个，支持只写关键字")
    p_done.add_argument(
        "--id", type=int, nargs="+", default=None,
        help="按编号标记（推荐，就是邮件里的 #12）；同一家公司有多条记录时用它才不会误标",
    )
    p_done.add_argument("--all", action="store_true", help="把当前所有未完成项都标记为完成")

    p_undo = sub.add_parser("undo", help="撤销「已完成」标记")
    p_undo.add_argument("companies", nargs="*")
    p_undo.add_argument("--all", action="store_true", help="撤销全部已完成标记")

    p_jobs = sub.add_parser("jobs", help="按截止时间由近到远列出未完成的面试/笔试")
    p_jobs.add_argument("--include-completed", action="store_true", help="连已完成的也列出来")

    p_list = sub.add_parser("list", help="列出已入库邮件")
    p_list.add_argument("--status", default=None, help="按状态过滤，如 new / pending_review / notified")
    p_list.add_argument("--limit", type=int, default=20)
    return parser


def _mask(secret: str) -> str:
    """只显示是否已设置，绝不打印凭据本身。"""
    return "已设置" if secret else "未设置"


def _cmd_check(cfg: Any) -> int:
    logger = get_logger("cli")
    tz = cfg.tzinfo(logger)
    print("=" * 62)
    print("招聘邮件通知系统 — 配置检查")
    print("=" * 62)
    print("项目根目录 : %s" % cfg.project_root)
    print("数据库     : %s" % cfg.db_path)
    print("日志目录   : %s" % cfg.log_dir)
    print("时区       : %s -> %s" % (cfg.notify.timezone, tz))
    print("")
    print("[IMAP 收信]")
    print("  服务器   : %s:%s  SSL=%s  只读=%s" % (cfg.imap.host, cfg.imap.port, cfg.imap.use_ssl, cfg.imap.readonly))
    print("  账号     : %s" % (cfg.imap.username or "（未设置）"))
    print("  授权码   : %s" % _mask(cfg.imap.password))
    print("  就绪     : %s" % ("是" if cfg.imap.is_ready() else "否"))
    print("")
    print("[SMTP 发信]")
    print("  服务器   : %s:%s  SSL=%s" % (cfg.smtp.host, cfg.smtp.port, cfg.smtp.use_ssl))
    print("  账号     : %s" % (cfg.smtp.username or "（未设置）"))
    print("  授权码   : %s" % _mask(cfg.smtp.password))
    print("  就绪     : %s" % ("是" if cfg.smtp.is_ready() else "否"))
    print("")
    print("[分类策略]")
    print("  策略     : %s   规则匹配范围: %s" % (cfg.classifier.strategy, cfg.classifier.rule_scope))
    print("  规则采信 >= %.2f ；模型触发 < %.2f ；待确认 < %.2f"
          % (cfg.classifier.rule_accept_confidence, cfg.classifier.llm_trigger_confidence,
             cfg.classifier.pending_review_confidence))
    print("  模型     : enabled=%s 就绪=%s  %s / %s  API Key=%s"
          % (cfg.model.enabled, cfg.model.is_ready(), cfg.model.base_url, cfg.model.model,
             _mask(cfg.model.api_key)))
    print("  规则类别 : %s" % ", ".join((cfg.rules.get("categories") or {}).keys()))
    print("")
    print("[汇总通知]")
    print("  每天     : %s（%s）" % (cfg.notify.daily_summary_time, cfg.notify.timezone))
    print("  轮询间隔 : 每 %d 分钟" % cfg.notify.poll_interval_minutes)
    print("  收件人   : %s" % (", ".join(cfg.notify.recipients) or "（未设置）"))
    print("  汇总类别 : %s" % ", ".join(cfg.notify.categories))
    print("")
    print("[保留期]")
    print("  只处理   : 最近 %s 天内到达的邮件" % (cfg.retention.max_age_days or "不限"))
    print("  清理超过 : %s 天的旧记录%s"
          % (cfg.retention.purge_after_days or "不限",
             "（启动时自动清理）" if cfg.retention.purge_on_start else ""))
    print("=" * 62)
    ok = cfg.imap.is_ready() or cfg.smtp.is_ready()
    if not cfg.imap.is_ready():
        print("提示：IMAP 未就绪，先照 .env.example 建好 .env 再跑 poll / run。")
    return 0 if ok else 1


def _cmd_classify_eml(args: argparse.Namespace, cfg: Any) -> int:
    logger = get_logger("cli")
    pipeline = Pipeline(config=cfg, logger=logger)
    pipeline.init_db()

    # 复用 ImapReader 的解析逻辑；_parse 不联网，仅做 MIME 解码
    reader = ImapReader(cfg.imap, logger=None, tz=cfg.tzinfo(logger))
    mails = []
    for index, path in enumerate(args.paths, 1):
        file_path = Path(path)
        if not file_path.exists():
            print("找不到文件：%s" % file_path)
            return 2
        mails.append(reader._parse(900000 + index, file_path.read_bytes(), None))

    if args.no_store:
        for mail in mails:
            result = classify_mail(mail, cfg, llm_client=None, logger=logger)
            _print_classification(mail, result)
        return 0

    stats = pipeline.ingest_mails(mails)
    print("导入完成：新入库 %d 封，错误 %d 个\n" % (stats["inserted"], stats["errors"]))
    for mail in mails:
        row = pipeline.db.get_message_by_message_id(mail.message_id)
        _print_row(row)
    return 0


def _print_classification(mail: Any, result: Any) -> None:
    print("-" * 62)
    print("主题   : %s" % (mail.subject or "（无主题）"))
    print("发件人 : %s" % (mail.sender or "（未知）"))
    print("分类   : %s（%s）  置信度 %.2f  来源 %s"
          % (result.category, result.label, result.confidence, result.source))
    print("公司   : %s" % (result.company or "未识别"))
    print("通知时间: %s" % result.notify_time)
    print("面试/笔试时间: %s" % (result.event_time or result.event_time_raw or "未识别"))
    print("截止时间: %s" % (result.deadline or "未识别"))
    print("状态   : %s%s" % (result.status, "（待确认）" if result.needs_review else ""))
    print("摘要   : %s" % result.summary)
    if result.reasons:
        print("依据   : %s" % "；".join(result.reasons))


def _print_row(row: Any) -> None:
    if row is None:
        print("（无记录）")
        return
    print("-" * 62)
    print("#%s [%s] %s | %s"
          % (row["id"], row["status"], row["category_label"] or row["category"],
             row["company"] or "未识别公司"))
    print("主题   : %s" % (row["subject"] or "（无主题）"))
    if row["position"]:
        print("岗位   : %s" % row["position"])
    if row["location"]:
        print("地点   : %s" % row["location"])
    print("置信度 : %.2f（%s）" % (row["confidence"], row["source"]))
    print("截止时间: %s" % (row["deadline"] or "未识别"))
    print("通知时间: %s" % row["notify_time"])
    print("面试/笔试时间: %s" % (row["event_time"] or row["event_time_raw"] or "未识别"))
    print("摘要   : %s" % (row["summary"] or ""))
    print("链接   : %s" % row["mail_link"])
    print("message_id: %s" % row["message_id"])


def _cmd_archive(args: argparse.Namespace, cfg: Any, db: Database) -> int:
    """把数据库里已入库、还没归档过的招聘通知复制到归档文件夹。"""
    import logging

    logger = logging.getLogger("rmn.cli")
    folder = (getattr(cfg.imap, "archive_folder", "") or "").strip()
    if not folder:
        print("mail.ini 的 [imap] archive_folder 是空的，无法归档。")
        return 2
    uids = db.list_unarchived_uids(cfg.notify.categories, cfg.imap.mailbox)
    if not uids:
        print("没有需要归档的邮件（都归档过了）。")
        return 0

    print("准备把 %d 封邮件复制到文件夹「%s」…" % (len(uids), folder))
    reader = ImapReader(cfg.imap, logger)
    try:
        copied = reader.copy_to_folder(uids, folder)
    finally:
        closer = getattr(reader, "close", None)
        if callable(closer):
            closer()
    marked = db.mark_archived(uids)
    print("已复制 %d 封，标记归档 %d 条。" % (copied, marked))
    print("去邮箱里看文件夹「%s」即可（原邮件仍在收件箱）。" % folder)
    return 0



def _cmd_pending(args: argparse.Namespace, db: Database) -> int:
    rows = db.list_pending_review()[: max(1, args.limit)]
    if not rows:
        print("没有待确认的邮件。")
        return 0
    print("待确认邮件 %d 封（置信度偏低，需要人工判断）：" % len(rows))
    for row in rows:
        print(" · #%s [%s %.2f] %s | %s"
              % (row["id"], row["category"], row["confidence"],
                 row["company"] or "未识别公司", row["subject"]))
        print("   %s" % row["mail_link"])
    return 0


def _cmd_done(args: argparse.Namespace, cfg: Any, db: Database) -> int:
    rows = []
    seen = set()
    if args.all:
        rows = db.list_pending_jobs(cfg.notify.categories, include_notified=True)
    else:
        if not args.companies and not args.id:
            print("请给出编号（--id 12）、公司名，或用 --all 标记全部。")
            return 2
        if args.id:
            wanted = {int(item) for item in args.id}
            for row in db.list_pending_jobs(cfg.notify.categories, include_notified=True):
                if int(row["id"]) in wanted and row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
        for keyword in args.companies or []:
            for row in db.find_jobs_by_company(keyword, cfg.notify.categories):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
    if not rows:
        print("没有匹配到未完成的记录。")
        return 1
    count = db.mark_completed([row["id"] for row in rows], by="cli")
    print("已标记完成 %d 条（以后不再提醒）：" % count)
    for row in rows:
        print("  · %s | %s" % (row["company"] or "未识别公司", row["subject"]))
    return 0


def _cmd_undo(args: argparse.Namespace, cfg: Any, db: Database) -> int:
    rows = []
    if args.all:
        with db.session() as conn:
            rows = list(conn.execute("SELECT * FROM messages WHERE completed_at IS NOT NULL").fetchall())
    else:
        if not args.companies:
            print("请给出公司名，或用 --all 撤销全部。")
            return 2
        seen = set()
        for keyword in args.companies:
            for row in db.find_jobs_by_company(keyword, cfg.notify.categories, include_completed=True):
                if row["completed_at"] and row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
    if not rows:
        print("没有匹配到已完成的记录。")
        return 1
    count = db.mark_undone([row["id"] for row in rows])
    print("已撤销完成标记 %d 条（会重新出现在提醒里）：" % count)
    for row in rows:
        print("  · %s | %s" % (row["company"] or "未识别公司", row["subject"]))
    return 0


def _cmd_jobs(args: argparse.Namespace, cfg: Any, db: Database, logger: Any) -> int:
    if args.include_completed:
        with db.session() as conn:
            rows = list(conn.execute(
                "SELECT * FROM messages WHERE category IN (%s) "
                "ORDER BY (deadline IS NULL), deadline ASC, id ASC"
                % ",".join("?" for _ in cfg.notify.categories),
                list(cfg.notify.categories),
            ).fetchall())
    else:
        rows = db.list_pending_jobs(cfg.notify.categories, include_notified=True)

    if not rows:
        print("没有未完成的面试/笔试。")
        return 0

    tz = cfg.tzinfo(logger)
    print("共 %d 条（按截止时间由近到远）：" % len(rows))
    for row in rows:
        tick = "✓" if row["completed_at"] else " "
        deadline = format_deadline(row["deadline"], tz) if row["deadline"] else "未识别"
        print(
            "#%-5s [%s] %-12s %-16s 截止 %-30s 提醒%d次"
            % (row["id"], tick, row["category"], (row["company"] or "未识别")[:14],
               deadline, row["reminder_count"] or 0)
        )
        print("      %s" % (row["subject"] or "（无主题）"))
        print("      %s" % row["mail_link"])
    return 0


def _cmd_replies(args: argparse.Namespace, db: Database) -> int:
    with db.session() as conn:
        rows = list(conn.execute(
            "SELECT * FROM messages WHERE category = 'reply' ORDER BY id DESC LIMIT ?",
            (max(1, args.limit),),
        ).fetchall())
    if not rows:
        print("还没有处理过任何完成回复。")
        print("提示：回复汇总邮件后，下一轮 poll 才会生效；summary 现在也会先收一次信。")
        return 0
    print("处理过的完成回复（最近 %d 条）：" % len(rows))
    for row in rows:
        print("-" * 66)
        print("时间 : %s" % row["received_at"])
        print("主题 : %s" % row["subject"])
        print("正文 : %s" % (row["summary"] or "（空）"))
        print("结果 : %s" % (row["reasons"] or ""))
    return 0


def _cmd_list(args: argparse.Namespace, db: Database) -> int:
    sql = "SELECT * FROM messages"
    params: List[Any] = []
    if args.status:
        sql += " WHERE status = ?"
        params.append(args.status)
    sql += " ORDER BY COALESCE(received_at, created_at) DESC, id DESC LIMIT ?"
    params.append(max(1, args.limit))
    with db.session() as conn:
        rows = list(conn.execute(sql, params).fetchall())
    if not rows:
        print("没有符合条件的记录。")
        return 0
    for row in rows:
        print("#%-5s %-14s %-12s %-8.2f %-18s %s"
              % (row["id"], row["status"], row["category"], row["confidence"],
                 (row["company"] or "未识别")[:16], (row["subject"] or "")[:48]))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    cfg = load_config(logger=None)
    setup_logging(cfg.log_dir, args.log_level)
    logger = get_logger("cli")
    db = Database(cfg.db_path)
    db.init_schema()

    if args.command == "check":
        return _cmd_check(cfg)

    if args.command == "init-db":
        logger.info("数据库已初始化：%s", cfg.db_path)
        print("数据库已初始化：%s" % cfg.db_path)
        print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "stats":
        print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "archive":
        return _cmd_archive(args, cfg, db)

    if args.command == "purge":
        pipeline = Pipeline(config=cfg, logger=logger)
        result = pipeline.run_purge(days=args.days, force=args.force)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "pending":
        return _cmd_pending(args, db)

    if args.command == "replies":
        return _cmd_replies(args, db)

    if args.command == "list":
        return _cmd_list(args, db)

    if args.command == "classify-eml":
        return _cmd_classify_eml(args, cfg)

    if args.command == "done":
        return _cmd_done(args, cfg, db)

    if args.command == "undo":
        return _cmd_undo(args, cfg, db)

    if args.command == "jobs":
        return _cmd_jobs(args, cfg, db, logger)

    if args.command == "poll":
        pipeline = Pipeline(config=cfg, logger=logger, dry_run=args.dry_run)
        result = pipeline.run_poll()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "summary":
        pipeline = Pipeline(config=cfg, logger=logger, dry_run=args.dry_run)
        result = pipeline.run_summary(
            force=args.force, poll_first=not args.no_poll_first
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "run":
        # 不传 config，交给 Pipeline 每次运行重新读两个 INI
        pipeline = Pipeline(logger=logger, dry_run=args.dry_run)
        if args.watch:
            scheduler = Scheduler(pipeline, logger)
            scheduler.run_forever(
                poll_on_start=not args.no_poll_on_start,
                summary_on_start=args.summary_on_start,
            )
            return 0

        result = pipeline.run_once()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["poll"].get("ok") else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())