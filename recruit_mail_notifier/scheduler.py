# -*- coding: utf-8 -*-
"""调度器：每 N 分钟轮询新邮件，每天固定时刻发送汇总。

只用标准库实现（不引入 APScheduler）。时刻按 mail.ini 里 notify.timezone
指定的时区计算，所以「每天早上 10:00」跟机器本地时区无关。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from .logging_setup import get_logger


class Scheduler:
    def __init__(self, pipeline: Any, logger: Any = None) -> None:
        self.pipeline = pipeline
        self.logger = logger or get_logger("scheduler")

    # ------------------------------------------------------------ 时间计算
    def _tz(self) -> Any:
        return self.pipeline.cfg.tzinfo(self.logger)

    def next_summary_at(self, now: Optional[datetime] = None) -> datetime:
        tz = self._tz()
        now = now or datetime.now(tz)
        hour, minute = self.pipeline.cfg.notify.summary_hour_minute
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate = candidate + timedelta(days=1)
        return candidate

    # ------------------------------------------------------------ 任务包装
    def run_poll_once(self) -> Dict[str, Any]:
        try:
            return self.pipeline.run_poll()
        except Exception:
            self.logger.exception("轮询任务异常")
            return {"ok": False}

    def run_purge_once(self) -> Dict[str, Any]:
        try:
            return self.pipeline.run_purge()
        except Exception:
            self.logger.exception("保留期清理异常")
            return {"ok": False}

    def run_summary_once(self) -> Dict[str, Any]:
        try:
            return self.pipeline.run_summary()
        except Exception:
            self.logger.exception("汇总任务异常")
            return {"ok": False}

    # ------------------------------------------------------------ 主循环
    def run_forever(
        self, poll_on_start: bool = True, summary_on_start: bool = False, sleep_seconds: float = 5.0
    ) -> None:
        cfg = self.pipeline.cfg
        tz = self._tz()
        now = datetime.now(tz)
        hour, minute = cfg.notify.summary_hour_minute

        self.logger.info(
            "调度启动：每 %d 分钟轮询一次；每天 %02d:%02d（%s）发送汇总；数据库 %s",
            cfg.notify.poll_interval_minutes, hour, minute, cfg.notify.timezone, cfg.db_path,
        )

        # 启动第一件事：清掉数据库里超过保留期的旧记录
        if cfg.retention.purge_on_start:
            self.run_purge_once()

        if poll_on_start:
            self.run_poll_once()
        if summary_on_start:
            self.run_summary_once()

        # 启动时若今天已经过了汇总时刻，就不再补发，等明天
        already_past = (now.hour, now.minute) >= (hour, minute)
        last_summary_date = now.date() if already_past else None
        last_purge_date = now.date()
        next_poll_at = time.monotonic() + max(1, int(cfg.notify.poll_interval_minutes)) * 60
        self.logger.info("下一次汇总时间：%s", self.next_summary_at(now).isoformat())

        try:
            while True:
                time.sleep(max(1.0, float(sleep_seconds)))
                now = datetime.now(self._tz())

                if time.monotonic() >= next_poll_at:
                    self.run_poll_once()
                    # 每轮重新读一次间隔，改 mail.ini 后无需重启
                    interval = max(1, int(self.pipeline.cfg.notify.poll_interval_minutes)) * 60
                    next_poll_at = time.monotonic() + interval

                # 电脑常年不关的话，每天也清一次
                if last_purge_date != now.date():
                    self.run_purge_once()
                    last_purge_date = now.date()

                hour, minute = self.pipeline.cfg.notify.summary_hour_minute
                if last_summary_date != now.date() and (now.hour, now.minute) >= (hour, minute):
                    self.run_summary_once()
                    last_summary_date = now.date()
                    self.logger.info("下一次汇总时间：%s", self.next_summary_at(now).isoformat())
        except KeyboardInterrupt:
            self.logger.info("收到中断信号，调度停止。")