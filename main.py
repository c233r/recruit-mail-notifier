#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""招聘邮件通知系统入口。

用法示例：
    python main.py check
    python main.py classify-eml tests\fixtures\sample_interview.eml
    python main.py poll
    python main.py summary --dry-run
    python main.py run
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from recruit_mail_notifier.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())