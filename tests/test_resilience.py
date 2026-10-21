import ssl
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch
from radar.model import Gemini
from radar.public_sources import Window, parse_austms
from radar.engine import Engine
from test_engine import BODY, SOURCE
import test_engine as fixtures

class ResilienceTests(unittest.TestCase):
    def test_transient_public_read_retries_once(self):
        for error in (TimeoutError(), URLError(TimeoutError()), ConnectionResetError(),
                      HTTPError('https://example.edu.au', 503, 'busy', {}, None)):
            get = Mock(side_effect=[error, b'good'])
            with patch('radar.public_sources.time.sleep'):
                result = Window({'allowed_hosts': ['example.edu.au']}, get).read('https://example.edu.au')
            self.assertEqual(result, b'good')
            self.assertEqual(get.call_count, 2)

    def test_retry_exhaustion_is_failure_not_empty_inventory(self):
        get = Mock(side_effect=TimeoutError())
        with patch('radar.public_sources.time.sleep'), self.assertRaises(TimeoutError):
            Window({'allowed_hosts': []}, get).read('https://example.edu.au')
        self.assertEqual(get.call_count, 2)

    def test_security_and_parser_failures_are_not_retried(self):
        for error in (URLError(ssl.SSLCertVerificationError()), ValueError('identity mismatch'),
                      HTTPError('https://example.edu.au', 403, 'challenge', {}, None),
                      HTTPError('https://example.edu.au', 429, 'limit', {}, None),
                      HTTPError('https://example.edu.au', 302, 'redirect', {}, None)):
            get = Mock(side_effect=error)
            with self.assertRaises(type(error)):
                Window({'allowed_hosts': []}, get).read('https://example.edu.au')
            self.assertEqual(get.call_count, 1)

    def test_timeout_does_not_extend_original_source_deadline(self):
        clock = [0]
        def get(*args, **kwargs):
            clock[0] = 4
            raise TimeoutError()
        reader = Mock(side_effect=get)
        with patch('radar.public_sources.time.monotonic', side_effect=lambda: clock[0]), \
                patch('radar.public_sources.time.sleep') as sleep:
            window = Window({'allowed_hosts': []}, reader, seconds=5)
            with self.assertRaises(TimeoutError):
                window.read('https://example.edu.au')
        self.assertEqual(reader.call_count, 1)
        sleep.assert_not_called()

    def test_austms_requires_explicitly_reviewed_new_host(self):
        source = {**SOURCE, 'url': 'https://austms.org.au/resources/austms-careers/',
                  'allowed_hosts': ['austms.org.au']}
        html = ('<div class="brxe-50eff4"><h3>Research Fellow @ Flinders University</h3>'
                '<div class="x-read-more_content">' + BODY + '</div>'
                '<a href="https://www.flinders.edu.au/jobs/123">APPLY</a></div>')
        with self.assertRaises(ValueError):
            parse_austms(source, html)
        source['allowed_hosts'].append('www.flinders.edu.au')
        items, complete = parse_austms(source, html)
        self.assertEqual(len(items), 1)
        self.assertFalse(complete)
        with self.assertRaises(ValueError):
            parse_austms(source, html.replace('www.flinders.edu.au', 'www.flinders.edu.au.evil.test'))

    def test_model_timeout_is_single_request_without_sensitive_details(self):
        session = Mock()
        session.post.side_effect = TimeoutError('private request data')
        model = Gemini('radar-test-project', 'global', 'gemini-test', session=session)
        fixture = fixtures.EngineTests(); fixture.setUp()
        with self.assertRaisesRegex(RuntimeError, 'TimeoutError') as result:
            model.analyze(fixture.items[0], fixture.profile)
        self.assertNotIn('private', str(result.exception))
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(session.post.call_args.kwargs['timeout'], (5, 75))

    def test_engine_reserves_time_before_starting_a_model_request(self):
        fixture = fixtures.EngineTests(); fixture.setUp()
        engine = Engine(fixture.store, fixture.model, fixture.sender, -1001,
                        fixture.profile, now=lambda: fixture.timestamp,
                        monotonic=lambda: 0, soft_seconds=90)
        engine.acquire()
        engine.ingest(SOURCE, fixture.items[:1], True)
        engine.evaluate()
        engine.release()
        self.assertEqual(fixture.model.calls, 0)
        self.assertEqual(next(iter(fixture.store.scan('jobs').values()))['state'], 'pending')
