# -*- coding: utf-8 -*-
"""归档文件夹相关行为。"""

import unittest

from recruit_mail_notifier.config import ImapConfig
from recruit_mail_notifier.mail_reader import ImapReader, encode_imap_utf7


class _FakeConn:
    def __init__(self):
        self.calls = []

    def create(self, name):
        self.calls.append(("CREATE", name))
        return ("OK", [b""])

    def uid(self, command, *args):
        self.calls.append((command, tuple(args)))
        return ("OK", [b""])

    def select(self, name, readonly=True):
        self.calls.append(("SELECT", name, readonly))
        return ("OK", [b"1"])


def _reader(archive_folder="招聘通知", readonly=True):
    cfg = ImapConfig(
        host="h", username="u", password="p",
        readonly=readonly, archive_folder=archive_folder,
    )
    reader = ImapReader(cfg)
    reader._conn = _FakeConn()
    return reader


class ArchiveTest(unittest.TestCase):
    def test_readonly_is_forced_off_only_when_archiving(self):
        self.assertFalse(_reader(archive_folder="招聘通知", readonly=True)._select_readonly())
        self.assertTrue(_reader(archive_folder="", readonly=True)._select_readonly())
        self.assertFalse(_reader(archive_folder="", readonly=False)._select_readonly())

    def test_copy_creates_folder_then_copies(self):
        reader = _reader()
        copied = reader.copy_to_folder([11, 12, 13], "招聘通知")
        self.assertEqual(copied, 3)
        kinds = [call[0] for call in reader._conn.calls]
        self.assertIn("CREATE", kinds)
        self.assertIn("COPY", kinds)
        copy_call = [c for c in reader._conn.calls if c[0] == "COPY"][0]
        self.assertEqual(copy_call[1], ("11,12,13", '"%s"' % encode_imap_utf7("招聘通知")))

    def test_non_ascii_folder_is_encoded_to_ascii(self):
        encoded = encode_imap_utf7("招聘通知")
        self.assertTrue(encoded.isascii(), "发给 IMAP 的文件夹名必须是 ASCII")
        self.assertTrue(encoded.startswith("&") and encoded.endswith("-"))
        self.assertEqual(encode_imap_utf7("INBOX"), "INBOX")
        self.assertEqual(encode_imap_utf7("A&B"), "A&-B")

    def test_copy_splits_into_batches_of_fifty(self):
        reader = _reader()
        reader.copy_to_folder(list(range(1, 121)), "招聘通知")
        copies = [c for c in reader._conn.calls if c[0] == "COPY"]
        self.assertEqual(len(copies), 3)

    def test_copy_without_folder_does_nothing(self):
        reader = _reader(archive_folder="")
        self.assertEqual(reader.copy_to_folder([1, 2], ""), 0)
        self.assertEqual(reader._conn.calls, [])


if __name__ == "__main__":
    unittest.main()
