# -*- coding: utf-8 -*-
"""环境变量覆盖 mail.ini 的优先级。"""

import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from recruit_mail_notifier import config as config_mod
from recruit_mail_notifier.config import load_config

MAIL_INI = """
[imap]
host = imap.from-ini
port = 993
mailbox = INBOX

[smtp]
host = smtp.from-ini
port = 465

[notify]
recipients = ini@example.com
timezone = Asia/Shanghai
max_items_per_summary = 50
push_only_when_changed = true

[retention]
max_age_days = 30
"""

MODEL_INI = """
[classifier]
strategy = llm_only
"""


class EnvOverrideTest(unittest.TestCase):
    def setUp(self):
        # 不读本机真实的 .env，保证测试结果与本机配置无关
        self._saved_loaded = config_mod._ENV_LOADED
        config_mod._ENV_LOADED = True
        self._saved_env = {k: v for k, v in os.environ.items() if k.startswith("RECRUIT_")}
        for key in list(os.environ):
            if key.startswith("RECRUIT_"):
                del os.environ[key]

        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.mail = base / "mail.ini"
        self.mail.write_text(textwrap.dedent(MAIL_INI), encoding="utf-8")
        self.model = base / "model.ini"
        self.model.write_text(textwrap.dedent(MODEL_INI), encoding="utf-8")
        self.rules = base / "rules.json"

    def tearDown(self):
        config_mod._ENV_LOADED = self._saved_loaded
        for key in list(os.environ):
            if key.startswith("RECRUIT_"):
                del os.environ[key]
        os.environ.update(self._saved_env)
        self._tmp.cleanup()

    def _load(self):
        return load_config(mail_ini=self.mail, model_ini=self.model, rules_json=self.rules)

    def test_ini_is_used_when_no_env(self):
        cfg = self._load()
        self.assertEqual(cfg.imap.host, "imap.from-ini")
        self.assertEqual(cfg.smtp.host, "smtp.from-ini")
        self.assertEqual(cfg.notify.recipients, ["ini@example.com"])

    def test_env_beats_ini(self):
        os.environ["RECRUIT_IMAP_HOST"] = "imap.from-env"
        os.environ["RECRUIT_SMTP_HOST"] = "smtp.from-env"
        os.environ["RECRUIT_NOTIFY_RECIPIENTS"] = "env@example.com"
        cfg = self._load()
        self.assertEqual(cfg.imap.host, "imap.from-env")
        self.assertEqual(cfg.smtp.host, "smtp.from-env")
        self.assertEqual(cfg.notify.recipients, ["env@example.com"])

    def test_blank_env_falls_back_to_ini(self):
        os.environ["RECRUIT_NOTIFY_RECIPIENTS"] = "   "
        cfg = self._load()
        self.assertEqual(cfg.notify.recipients, ["ini@example.com"])

    def test_multiple_recipients(self):
        os.environ["RECRUIT_NOTIFY_RECIPIENTS"] = "a@example.com, b@example.com"
        cfg = self._load()
        self.assertEqual(cfg.notify.recipients, ["a@example.com", "b@example.com"])

    def test_bool_and_int_overrides(self):
        os.environ["RECRUIT_NOTIFY_PUSH_ONLY_WHEN_CHANGED"] = "false"
        os.environ["RECRUIT_NOTIFY_MAX_ITEMS"] = "7"
        os.environ["RECRUIT_IMAP_PORT"] = "1993"
        cfg = self._load()
        self.assertFalse(cfg.notify.push_only_when_changed)
        self.assertEqual(cfg.notify.max_items_per_summary, 7)
        self.assertEqual(cfg.imap.port, 1993)

    def test_timezone_override(self):
        os.environ["RECRUIT_NOTIFY_TIMEZONE"] = "Asia/Tokyo"
        cfg = self._load()
        self.assertEqual(cfg.notify.timezone, "Asia/Tokyo")

    def test_imap_user_override(self):
        os.environ["RECRUIT_IMAP_USER"] = "someone@example.com"
        cfg = self._load()
        self.assertEqual(cfg.imap.username, "someone@example.com")


if __name__ == "__main__":
    unittest.main()
