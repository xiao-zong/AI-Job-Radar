import json
import unittest
from unittest.mock import Mock, patch

from radar.icloud_reader import MailAccessError
from radar.mail_sources import fetch_icloud, ResolutionDeferred, Resolver
from radar.store import MemoryStore
from test_mail_sources import raw_mail, SOURCE, RECEIVED


class MailCacheTests(unittest.TestCase):
    def setUp(self):
        self.source = {**SOURCE, "folder": "AI Job Radar", "not_before": RECEIVED - 100}
        self.reader, self.cache = Mock(), MemoryStore()

    def test_repeated_poll_uses_persisted_cards_without_resolving_again(self):
        self.reader.messages.return_value = [{"raw": raw_mail(), "received_at": RECEIVED}]
        first, complete = fetch_icloud(self.source, reader=self.reader, cache=self.cache)
        with patch("radar.mail_sources.parse_alert") as parser:
            again, _ = fetch_icloud(self.source, reader=self.reader, cache=self.cache)
        parser.assert_not_called()
        self.assertFalse(complete)
        self.assertEqual(first, again)
        self.assertEqual(again.last_alert_received_at, RECEIVED)
        self.assertTrue(all(j["evidence_received_at"] == RECEIVED for j in again))
        private = json.dumps(self.cache.data)
        self.assertNotIn("private.person", private)
        self.assertNotIn("trackingId", private)
        self.assertNotIn("raw\"", private)

    def test_allowance_defers_old_mail_and_next_poll_resumes_after_cached_successes(self):
        self.reader.messages.return_value = [
            {"raw": str(i).encode(), "received_at": RECEIVED - i} for i in range(6)]
        parsed = []
        def parse(raw, source, received, resolver):
            if resolver.requests >= 2:
                raise ResolutionDeferred("full")
            resolver.requests += 1
            parsed.append(raw)
            return [{"id": raw.decode(), "evidence_received_at": received}]
        with patch("radar.mail_sources.parse_alert", side_effect=parse):
            for expected in (2, 4, 6):
                jobs, _ = fetch_icloud(self.source, reader=self.reader, cache=self.cache,
                                      resolver=Resolver())
                self.assertEqual(len(jobs), expected)
                if expected < 6:
                    self.assertIn("mail_resolution_deferred", jobs.health_warnings)
            self.assertEqual(jobs.health_warnings, [])
        self.assertEqual(len(parsed), 6)  # Each successful message parsed just once.
        self.assertEqual([j["id"] for j in jobs], [str(i) for i in range(6)])

    def test_bad_message_does_not_discard_valid_mail_and_coverage_is_visible(self):
        self.reader.messages.return_value = [
            {"raw": b"bad", "received_at": RECEIVED},
            {"raw": raw_mail(), "received_at": RECEIVED - 5}]
        from radar.mail_sources import parse_alert
        def parse(raw, *args):
            if raw == b"bad":
                raise MailAccessError("Unsupported template")
            return parse_alert(raw, *args)
        with patch("radar.mail_sources.parse_alert", side_effect=parse):
            jobs, _ = fetch_icloud(self.source, reader=self.reader, cache=self.cache)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs.health_warnings, ["mail_message_requires_review"])
        self.assertEqual(jobs.last_alert_received_at, RECEIVED)
        self.assertTrue(all(j["evidence_received_at"] == RECEIVED - 5 for j in jobs))

    def test_all_unsupported_mail_is_still_a_source_failure(self):
        self.reader.messages.return_value = [{"raw": b"bad", "received_at": RECEIVED}]
        with patch("radar.mail_sources.parse_alert", side_effect=MailAccessError("bad")):
            with self.assertRaises(MailAccessError):
                fetch_icloud(self.source, reader=self.reader, cache=self.cache)

    def test_mailbox_or_cache_storage_failure_is_not_hidden_as_warning(self):
        self.reader.messages.side_effect = MailAccessError("login failed")
        with self.assertRaises(MailAccessError):
            fetch_icloud(self.source, reader=self.reader, cache=self.cache)
        self.reader.messages.side_effect = None
        self.reader.messages.return_value = [{"raw": raw_mail(), "received_at": RECEIVED}]
        broken = Mock()
        broken.get.side_effect = RuntimeError("storage unavailable")
        with self.assertRaises(RuntimeError):
            fetch_icloud(self.source, reader=self.reader, cache=broken)

    def test_parser_revision_or_different_receipt_never_reuses_unmatched_cache(self):
        self.reader.messages.return_value = [{"raw": raw_mail(), "received_at": RECEIVED}]
        fetch_icloud(self.source, reader=self.reader, cache=self.cache)
        self.reader.messages.return_value[0]["received_at"] = RECEIVED + 20
        changed, _ = fetch_icloud(self.source, reader=self.reader, cache=self.cache)
        self.assertEqual(len(self.cache.data), 2)
        self.assertTrue(all(j["evidence_received_at"] == RECEIVED + 20 for j in changed))
        with patch("radar.mail_sources.CACHE_VERSION", "future_parser"):
            fetch_icloud(self.source, reader=self.reader, cache=self.cache)
        self.assertEqual(len(self.cache.data), 3)
        self.assertTrue(all("health_warnings" not in record for record in self.cache.data.values()))

    def test_empty_mailbox_needs_no_cache_or_cloud_client(self):
        self.reader.messages.return_value = []
        with patch("radar.store.FirestoreStore") as store:
            jobs, _ = fetch_icloud(self.source, reader=self.reader)
        store.assert_not_called()
        self.assertEqual(jobs, [])
        self.assertEqual(jobs.last_alert_received_at, 0)


if __name__ == "__main__":
    unittest.main()
