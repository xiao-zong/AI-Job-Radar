import unittest
from unittest.mock import Mock, patch

from radar.model import Gemini, ModelServiceError
import test_engine as fixtures


class ProviderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EngineTests()
        self.f.setUp()
        self.sleep = patch('radar.engine.time.sleep').start()
        patch('radar.engine.random.random', return_value=0).start()
        self.addCleanup(patch.stopall)

    def provider(self, effects):
        self.f.model.analyze = Mock(side_effect=effects)

    def success(self):
        return fixtures.assessment(), {'totalTokenCount': 10}

    def quota(self):
        return self.f.store.get('daily/2026-10-20')['model_calls']

    def test_429_then_success_reserves_both_calls_and_sends_once(self):
        self.provider([ModelServiceError('http_429', True, 429), self.success()])
        result, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        self.assertEqual(result['model_calls_this_run'], 2)
        self.assertEqual(self.quota(), 2)
        self.assertEqual(len(self.f.ordinary()), 1)
        self.assertEqual(self.f.ordinary()[0]['state'], 'sent')
        job = self.f.store.get('jobs/' + self.f.items[0]['id'])
        self.assertEqual((job['attempts'], job['service_failures'], job['state']), (2, 1, 'evaluated'))
        self.f.run_items(self.f.items[:1])
        self.assertEqual(self.f.model.analyze.call_count, 2)
        self.assertEqual(len(self.f.ordinary()), 1)

    def test_two_transient_failures_stop_other_calls_but_keep_warning_and_queue(self):
        self.provider([ModelServiceError('http_429', True, 429)] * 2)
        result, code = self.f.run_items(self.f.items[:3])
        self.assertEqual(code, 0)
        self.assertIn('model_service_failed:http_429', result['warnings'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.f.model.analyze.call_count, 2)
        self.assertEqual(self.quota(), 2)
        self.assertEqual(len(self.f.ordinary()), 0)
        self.assertTrue(all(j['state'] == 'pending' for j in self.f.store.scan('jobs').values()))

    def test_retry_never_exceeds_daily_quota(self):
        self.f.profile['model_calls_per_day_limit'] = 1
        self.provider([ModelServiceError('http_429', True, 429), self.success()])
        result, code = self.f.run_items(self.f.items[:2])
        self.assertEqual(code, 0)
        self.assertEqual(self.quota(), 1)
        self.assertEqual(self.f.model.analyze.call_count, 1)

    def test_retry_never_exceeds_per_run_quota(self):
        self.f.profile['model_calls_per_run_limit'] = 1
        self.provider([ModelServiceError('transport_ReadTimeout', True), self.success()])
        _, code = self.f.run_items(self.f.items[:2])
        self.assertEqual(code, 0)
        self.assertEqual(self.quota(), 1)
        self.assertEqual(self.f.model.analyze.call_count, 1)
        self.sleep.assert_not_called()

    def test_cooldown_survives_restart_and_later_recovery_is_automatic(self):
        self.provider([ModelServiceError('http_503', True, 503)] * 2 + [self.success()])
        self.f.run_items(self.f.items[:1])
        _, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        self.assertEqual(self.f.model.analyze.call_count, 2)
        self.f.timestamp += 3600
        _, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        self.assertEqual(self.f.model.analyze.call_count, 3)
        self.assertEqual(self.quota(), 3)
        self.assertEqual(len(self.f.ordinary()), 1)
        self.assertEqual(self.f.store.get('control/model_service')['retry_after'], 0)

    def test_persistent_service_failure_eventually_requires_review(self):
        self.provider([ModelServiceError('http_429', True, 429)] * 4)
        self.f.run_items(self.f.items[:1])
        self.f.timestamp += 3600
        _, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        job = self.f.store.get('jobs/' + self.f.items[0]['id'])
        self.assertEqual((job['attempts'], job['service_failures'], job['state']), (4, 4, 'review_required'))

    def test_nonretryable_configuration_failure_stops_immediately(self):
        self.provider([ModelServiceError('http_403', False, 403)])
        _, code = self.f.run_items(self.f.items[:3])
        self.assertEqual(code, 1)
        self.assertEqual(self.quota(), 1)
        self.assertEqual(self.f.model.analyze.call_count, 1)
        self.sleep.assert_not_called()

    def test_long_retry_after_defers_without_sleeping(self):
        self.provider([ModelServiceError('http_429', True, 429, retry_after=120)])
        _, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        self.assertEqual(self.quota(), 1)
        self.sleep.assert_not_called()

    def test_bad_evidence_after_service_recovery_remains_rejected(self):
        value, usage = self.success()
        value['requirements'][0]['quote'] = 'Invented experience that is not present.'
        self.provider([ModelServiceError('http_429', True, 429), (value, usage)])
        result, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        self.assertTrue(any(e.startswith('assessment_failed:') for e in result['warnings']))
        self.assertEqual(len(self.f.ordinary()), 0)
        self.assertEqual(self.quota(), 2)

    def test_safe_http_status_diagnostics_never_log_response_body(self):
        for status in (400, 401, 403, 408, 429, 500, 502, 503, 504):
            session = Mock()
            session.post.return_value = Mock(status_code=status, headers={'Retry-After': '120'},
                                             text='PRIVATE REQUEST CONTENT')
            model = Gemini('radar-test-project', 'global', 'gemini-test', session)
            with self.assertRaises(ModelServiceError) as caught:
                model.analyze(self.f.items[0], self.f.profile)
            error = caught.exception
            self.assertEqual(error.http_status, status)
            self.assertEqual(error.retryable, status in (408, 429, 500, 502, 503, 504))
            self.assertEqual(error.retry_after, 120)
            self.assertEqual(str(error), 'http_' + str(status))
            self.assertEqual(session.post.call_count, 1)

