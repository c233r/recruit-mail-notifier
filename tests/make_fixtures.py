# -*- coding: utf-8 -*-
"""把样例邮件导出为 tests/fixtures/*.eml，方便手动验证分类效果。

    python tests/make_fixtures.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests import sample_replies  # noqa: E402
from tests.sample_emails import write_eml_files  # noqa: E402

if __name__ == "__main__":
    for path in write_eml_files(ROOT / "tests" / "fixtures"):
        print("已生成：%s" % path)
    for path in sample_replies.write_eml_files(ROOT / "tests" / "fixtures"):
        print("已生成：%s" % path)