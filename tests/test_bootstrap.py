import gzip
import io
import json
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]
from radar.telegram import Telegram, TelegramError, DeliveryUnknown
from check_sources import validate_url, parse_greenhouse, parse_rss, decode_response


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def open(self, request, timeout):
        self.calls.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.response).encode())


class BootstrapTests(unittest.TestCase):
    def test_rss1_namespace_and_root_items(self):
        body = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/">
        <channel><title>Jobs</title></channel>
        <item><title>Researcher</title><description>Details</description><link>https://good.test/job/1</link></item></rdf:RDF>'''
        self.assertEqual(parse_rss(body, ["good.test"])["count"], 1)

    def test_gzip_source_response_is_decoded_even_without_header(self):
        body = b'<rss><channel><title>Jobs</title></channel></rss>'
        for encoding in ("", "gzip"):
            decoded = decode_response(gzip.compress(body), encoding)
            self.assertEqual(parse_rss(decoded, [])["count"], 0)

    def test_decompressed_response_limit(self):
        with patch("check_sources.LIMIT", 100):
            with self.assertRaises(ValueError):
                decode_response(gzip.compress(b"a" * 1000), "gzip")

    def test_send_uses_fixed_destination_plaintext_no_preview(self):
        opener = Opener({"ok": True, "result": {"message_id": 42}})
        self.assertEqual(Telegram("123:TEST_ONLY", opener).send(-1001, "test <plain>"), 42)
        body = json.loads(opener.calls[0].data)
        self.assertEqual(body["chat_id"], -1001)
        self.assertNotIn("parse_mode", body)
        self.assertTrue(body["link_preview_options"]["is_disabled"])

    def test_timeout_is_unknown_and_not_retried_or_token_exposed(self):
        opener = Opener(error=TimeoutError("https://api.telegram.org/bot123:TEST_ONLY/sendMessage"))
        with self.assertRaises(DeliveryUnknown) as result:
            Telegram("123:TEST_ONLY", opener).send(-1001, "test")
        self.assertNotIn("TEST_ONLY", str(result.exception))
        self.assertEqual(len(opener.calls), 1)

    def test_rate_limit_fails_without_retry(self):
        error = urllib.error.HTTPError("https://api.telegram.org/bot123:TEST_ONLY/sendMessage", 429, "limit", {}, None)
        opener = Opener(error=error)
        with self.assertRaises(TelegramError) as result:
            Telegram("123:TEST_ONLY", opener).send(-1001, "test")
        self.assertNotIn("TEST_ONLY", str(result.exception))
        self.assertEqual(len(opener.calls), 1)

    def test_missing_message_id_is_unknown(self):
        with self.assertRaises(DeliveryUnknown):
            Telegram("123:TEST_ONLY", Opener({"ok": True, "result": {}})).send(-1001, "test")

    def test_invalid_target_or_long_message_never_sent(self):
        opener = Opener()
        client = Telegram("123:TEST_ONLY", opener)
        for target, text in [(1, "test"), (-1001, "😀" * 2000)]:
            with self.assertRaises(TelegramError):
                client.send(target, text)
        self.assertEqual(opener.calls, [])

    def test_allowlist_blocks_markdown_and_untrusted_urls(self):
        for url in ["[https://good.test](https://good.test)", "http://good.test", "https://good.test.evil.test", "https://user:pass@good.test", "https://good.test:8000"]:
            with self.assertRaises(ValueError):
                validate_url(url, ["good.test"])

    def test_partial_json_list_is_failure_not_zero_jobs(self):
        with self.assertRaises(ValueError):
            parse_greenhouse(b'{"jobs":[],"meta":{"total":1}}', [])
        self.assertEqual(parse_greenhouse(b'{"jobs":[],"meta":{"total":0}}', [])["count"], 0)

    def test_html_block_page_is_not_empty_rss(self):
        with self.assertRaises(ValueError):
            parse_rss(b'<html><body>Access denied</body></html>', [])

    def test_xml_entities_rejected(self):
        with self.assertRaises(ValueError):
            parse_rss(b'<!DOCTYPE rss [<!ENTITY a "b">]><rss/>', [])


if __name__ == "__main__":
    unittest.main()
