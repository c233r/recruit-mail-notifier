# -*- coding: utf-8 -*-
"""运行日志与错误日志。

* logs/run.log   : 全量运行日志，5MB 轮转，保留 5 份
* logs/error.log : 只记 ERROR 及以上，便于事后排查
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "rmn"
_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir, level: str = "INFO", console: bool = True) -> logging.Logger:
    """初始化日志。可重复调用，不会叠加 handler。"""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    if getattr(logger, "_rmn_configured", False):
        return logger
    logger._rmn_configured = True  # type: ignore[attr-defined]
    logger.propagate = False

    formatter = logging.Formatter(_FORMAT, _DATEFMT)

    run_handler = RotatingFileHandler(
        log_path / "run.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    run_handler.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    run_handler.setFormatter(formatter)
    logger.addHandler(run_handler)

    error_handler = RotatingFileHandler(
        log_path / "error.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)

    # pythonw.exe（无控制台窗口）下 sys.stdout 是 None，这时不装控制台 handler，
    # 日志照样会写进 logs/run.log
    if console and sys.stdout is not None:
        stream = sys.stdout
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
        console_handler = logging.StreamHandler(stream)
        console_handler.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


def get_logger(name: str = "") -> logging.Logger:
    """取子 logger，统一挂在 rmn 命名空间下。"""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
