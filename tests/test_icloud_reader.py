from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock

from radar.icloud_reader import ICloudReader, MailAccessError, MAX_BYTES


class ICloudReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "mail.json"
        self.path.write_text(json.dumps({
            "username": "test@icloud.com", "app_password": "aaaa-bbbb-cccc-dddd"}))
        self.now = datetime(2026, 10, 20, 5, tzinfo=timezone.utc).timestamp()
        self.mail = (b"From: jobs-noreply@linkedin.com\r\nSubject: New jobs: Data scientist\r\n"
                     b"Content-Type: text/plain\r\n\r\nMelbourne data scientist\r\n")
        self.client, self.factory = MagicMock(), MagicMock()
        self.factory.return_value = self.client
        self.client.select.return_value = ("OK", [b"1"])
        self.calls = []

        def uid(operation, *args):
            self.calls.append((operation, args))
            if operation == "search":
                return "OK", [b"1"]
            if args[1] == "(UID INTERNALDATE RFC822.SIZE)":
                return "OK", [b'1 (UID 1 INTERNALDATE "20-Oct-2026 04:00:00 +0000" RFC822.SIZE '
                              + str(len(self.mail)).encode() + b")"]
            return "OK", [(b"1 (BODY[]", self.mail), b")"]
        self.client.uid.side_effect = uid

    def tearDown(self):
        self.temp.cleanup()

    def read(self, source="linkedin_email"):
        return ICloudReader(self.path, factory=self.factory, now=lambda: self.now).messages(
            source, "AI Job Radar", self.now - 86400)

    def test_readonly_folder_peek_sender_and_time_boundaries(self):
        rows = self.read()
        self.assertEqual(len(rows), 1)
        self.client.select.assert_called_once_with('"AI Job Radar"', readonly=True)
        self.assertEqual(self.factory.call_args.args, ("imap.mail.me.com", 993))
        context = self.factory.call_args.kwargs["ssl_context"]
        self.assertTrue(context.check_hostname)
        search = self.calls[0][1][1]
        self.assertIn("jobs-noreply@linkedin.com", search)
        self.assertIn("SINCE", search)
        self.assertIn("BODY.PEEK[]", self.calls[-1][1][1])
        self.assertTrue(all(op in {"search", "fetch"} for op, _ in self.calls))
        self.client.close.assert_not_called()
        self.client.logout.assert_called_once()

    def test_forged_from_display_name_does_not_enter_parser(self):
        self.mail = self.mail.replace(
            b"From: jobs-noreply@linkedin.com",
            b'From: "jobs-noreply@linkedin.com" <attacker@example.org>')
        self.assertEqual(self.read(), [])

    def test_seek_generic_sender_only_allows_job_recommendations(self):
        self.mail = self.mail.replace(b"jobs-noreply@linkedin.com", b"noreply@s.seek.com.au")
        self.mail = self.mail.replace(b"New jobs: Data scientist", b"Your password reset")
        self.assertEqual(self.read("seek_email"), [])

    def test_empty_folder_is_success_without_body_reads(self):
        self.client.uid.side_effect = None
        self.client.uid.return_value = ("OK", [None])
        self.assertEqual(self.read(), [])
        self.assertEqual(self.client.uid.call_count, 1)

    def test_linkedin_application_receipt_is_not_a_job_alert(self):
        self.mail = self.mail.replace(b"New jobs: Data scientist", b"Your application was sent")
        self.assertEqual(self.read(), [])

    def test_missing_folder_has_no_inbox_fallback(self):
        self.client.select.return_value = ("NO", [b"missing"])
        with self.assertRaises(MailAccessError):
            self.read()
        self.client.uid.assert_not_called()

    def test_excess_volume_fails_without_silent_truncation(self):
        self.client.uid.side_effect = None
        self.client.uid.return_value = ("OK", [b" ".join(str(i).encode() for i in range(1, 102))])
        with self.assertRaises(MailAccessError):
            self.read()
        self.assertEqual(self.client.uid.call_count, 1)

    def test_oversize_mail_rejected_before_reading_body(self):
        self.client.uid.side_effect = [
            ("OK", [b"1"]),
            ("OK", [b'1 (UID 1 INTERNALDATE "20-Oct-2026 04:00:00 +0000" RFC822.SIZE '
                    + str(MAX_BYTES + 1).encode() + b")"]),
        ]
        with self.assertRaises(MailAccessError):
            self.read()
        self.assertEqual(self.client.uid.call_count, 2)

    def test_server_errors_do_not_expose_credentials(self):
        self.client.login.side_effect = RuntimeError("secret aaaa-bbbb-cccc-dddd")
        with self.assertRaises(MailAccessError) as error:
            self.read()
        self.assertNotIn("aaaa", str(error.exception))

    def test_old_mail_not_fetched_or_reintroduced(self):
        self.client.uid.side_effect = [
            ("OK", [b"1"]),
            ("OK", [b'1 (UID 1 INTERNALDATE "10-Oct-2026 04:00:00 +0000" RFC822.SIZE 100)']),
        ]
        self.assertEqual(self.read(), [])
        self.assertEqual(self.client.uid.call_count, 2)


if __name__ == "__main__":
    unittest.main()
