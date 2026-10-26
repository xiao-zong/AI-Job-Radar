"""Operational health differs from safely withheld or deferred individual work."""
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import Mock, patch

from radar import main as entrypoint
from radar.model import ModelServiceError
from radar.telegram import DeliveryUnknown
import test_engine as fixtures


class RunHealthTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.EngineTests()
        self.f.setUp()
        self.sleep = patch('radar.engine.time.sleep').start()
        patch('radar.engine.random.random', return_value=0).start()
        self.addCleanup(patch.stopall)

    def put_health(self, health):
        path = 'control/model_service'
        self.f.store.atomic([path], lambda values: values.update({path: health}))

    def failures(self, count=2, retryable=True, code='http_503'):
        self.f.model.analyze = Mock(side_effect=[ModelServiceError(code, retryable)] * count)

    def success(self):
        return fixtures.assessment(), {'totalTokenCount': 10}

    def test_invalid_evidence_is_withheld_warning_and_updates_heartbeat(self):
        value, usage = self.success()
        value['requirements'][0]['quote'] = 'An invented claim not present in the vacancy.'
        self.f.model.analyze = Mock(return_value=(value, usage))
        result, code = self.f.run_items(self.f.items[:1])
        self.assertEqual((code, result['status'], result['errors']), (0, 'success', []))
        self.assertTrue(any(w.startswith('assessment_failed:') for w in result['warnings']))
        self.assertEqual(self.f.ordinary(), [])
        self.assertEqual(self.f.store.get('control/heartbeat')['last_success'], self.f.timestamp)
        self.assertIsNone(self.f.store.get('control/model_service'))
        job = self.f.store.get('jobs/' + self.f.items[0]['id'])
        self.assertEqual((job['state'], job['attempts']), ('pending', 1))
        self.assertEqual(job['retry_after'], self.f.timestamp + 86400)
        self.assertIn('invented', job['validation_response']['requirements'][0]['quote'])
        self.f.run_items(self.f.items[:1])
        self.assertEqual(self.f.model.analyze.call_count, 1)

    def test_runtime_unexpected_and_json_errors_remain_failures(self):
        for error in (RuntimeError('response parse failed'), KeyError('unexpected'),
                      json.JSONDecodeError('Invalid JSON', 'bad', 0)):
            with self.subTest(error=type(error).__name__):
                self.f = fixtures.EngineTests()
                self.f.setUp()
                self.f.model.analyze = Mock(side_effect=error)
                result, code = self.f.run_items(self.f.items[:1])
                self.assertEqual((code, result['status']), (1, 'partial_failure'))
                self.assertTrue(any(e.startswith('assessment_failed:') for e in result['errors']))
                self.assertEqual(result['warnings'], [])
                self.assertIsNone(self.f.store.get('control/heartbeat'))
                self.assertIsNone(self.f.store.get('control/model_service'))
                self.assertEqual(self.f.ordinary(), [])

    def test_success_before_later_provider_failure_is_preserved(self):
        self.f.model.analyze = Mock(side_effect=[self.success()] + [ModelServiceError('http_503', True)] * 2)
        result, code = self.f.run_items(self.f.items[:3])
        self.assertEqual(code, 0)
        self.assertEqual(result['model_calls_this_run'], 3)
        self.assertIn('model_service_failed:http_503', result['warnings'])
        health = self.f.store.get('control/model_service')
        self.assertEqual(health['last_success'], self.f.timestamp)
        self.assertEqual(health['first_failure'], self.f.timestamp)
        self.assertEqual(len(self.f.ordinary()), 1)

    def test_recent_actual_success_makes_retryable_failure_a_warning(self):
        self.put_health({'last_success': self.f.timestamp - 60, 'retry_after': 0})
        self.failures()
        result, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        self.assertEqual(result['errors'], [])
        self.assertIn('model_service_failed:http_503', result['warnings'])
        health = self.f.store.get('control/model_service')
        self.assertEqual(health['last_success'], self.f.timestamp - 60)
        self.assertEqual(health['first_failure'], self.f.timestamp)

    def test_outage_without_any_success_escalates_after_24_hours(self):
        self.failures(4)
        first, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        began = self.f.timestamp
        heartbeat = self.f.store.get('control/heartbeat')
        self.f.timestamp += 24 * 3600
        second, code = self.f.run_items(self.f.items[:1])
        self.assertEqual((code, second['status']), (1, 'partial_failure'))
        self.assertIn('model_service_failed:http_503', second['errors'])
        self.assertEqual(self.f.store.get('control/model_service')['first_failure'], began)
        self.assertEqual(self.f.store.get('control/heartbeat'), heartbeat)
        self.assertEqual(first['model_calls_this_run'], 2)
        self.assertEqual(second['model_calls_this_run'], 2)

    def test_old_actual_success_does_not_receive_a_new_outage_grace(self):
        self.put_health({'last_success': self.f.timestamp - 24 * 3600, 'retry_after': 0})
        self.failures()
        result, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 1)
        self.assertIn('model_service_failed:http_503', result['errors'])

    def test_success_resets_outage_anchor_and_survives_restart(self):
        self.put_health({'last_success': self.f.timestamp - 90000,
                         'first_failure': self.f.timestamp - 90000, 'retry_after': 0})
        self.f.model.analyze = Mock(return_value=self.success())
        _, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        health = self.f.store.get('control/model_service')
        self.assertEqual(health['last_success'], self.f.timestamp)
        self.assertIsNone(health['first_failure'])
        self.assertEqual(health['retry_after'], 0)
        self.f.timestamp += 3600
        self.failures()
        result, code = self.f.run_items(self.f.items[:2])
        self.assertEqual(code, 0)
        self.assertIn('model_service_failed:http_503', result['warnings'])
        self.assertEqual(self.f.store.get('control/model_service')['first_failure'], self.f.timestamp)

    def test_deliberate_cooldown_is_warning_without_call_or_quota(self):
        self.put_health({'code': 'http_503', 'retryable': True,
                         'first_failure': self.f.timestamp - 60,
                         'retry_after': self.f.timestamp + 600})
        self.f.model.analyze = Mock()
        result, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 0)
        self.assertIn('model_service_cooldown:http_503', result['warnings'])
        self.f.model.analyze.assert_not_called()
        self.assertEqual(result['model_calls_this_run'], 0)
        self.assertIsNotNone(self.f.store.get('control/heartbeat'))
        self.assertEqual(self.f.store.get('daily/2026-10-20')['model_calls'], 0)

    def test_nonretryable_provider_failure_is_immediate_error(self):
        self.put_health({'last_success': self.f.timestamp - 60, 'retry_after': 0})
        self.failures(1, False, 'http_403')
        result, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 1)
        self.assertIn('model_service_failed:http_403', result['errors'])
        self.assertEqual(self.f.model.analyze.call_count, 1)
        self.sleep.assert_not_called()

    def test_successful_empty_mail_read_is_stale_warning_not_failure(self):
        source = {'id': 'linkedin_email', 'kind': 'icloud_alert', 'enabled': True,
                  'not_before': self.f.timestamp - 4 * 86400}
        result, code = self.f.engine().run([source], lambda s: ([], False))
        self.assertEqual((code, result['status'], result['errors']), (0, 'success', []))
        self.assertIn('mail_not_received_72h:linkedin_email', result['warnings'])
        state = self.f.store.get('sources/linkedin_email')
        self.assertEqual(state['status'], 'no_recent_alert')
        self.assertEqual(state['last_success'], self.f.timestamp)
        self.assertIsNotNone(self.f.store.get('control/heartbeat'))

    def test_partial_mail_metadata_preserves_receipt_and_warning(self):
        class MailItems(list):
            health_warnings = ['deferred_messages']
            last_alert_received_at = self.f.timestamp
        source = {'id': 'seek_email', 'kind': 'icloud_alert', 'enabled': True,
                  'not_before': self.f.timestamp - 4 * 86400}
        result, code = self.f.engine().run([source], lambda s: (MailItems(), False))
        self.assertEqual(code, 0)
        self.assertEqual(result['warnings'], ['source_warning:seek_email:deferred_messages'])
        state = self.f.store.get('sources/seek_email')
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(state['last_alert_received_at'], self.f.timestamp)
        self.assertEqual(state['warnings'], ['deferred_messages'])

    def test_actual_source_read_failure_and_delivery_uncertainty_remain_errors(self):
        def fail(source):
            raise RuntimeError('safe source failure')
        result, code = self.f.engine().run([fixtures.SOURCE], fail)
        self.assertEqual(code, 1)
        self.assertIn('source_failed:test', result['errors'])
        self.assertIsNone(self.f.store.get('control/heartbeat'))
        self.f.sender.error = DeliveryUnknown()
        result, code = self.f.run_items(self.f.items[:1])
        self.assertEqual(code, 1)
        self.assertIn('delivery_uncertain', result['errors'])
        self.assertTrue(any(e['state'] == 'unknown' for e in self.f.ordinary()))


class EntrypointSeverityTests(unittest.TestCase):
    def invoke(self, summary, code):
        output = io.StringIO()
        config = {'project_id': 'radar-test-project', 'model_location': 'global',
                  'model_id': 'gemini-test', 'chat_id': -1001}
        with ExitStack() as stack:
            stack.enter_context(patch('radar.main.Path.read_text', side_effect=[json.dumps(config), '{}', '[]']))
            stack.enter_context(patch('radar.main.config_check', return_value=datetime(2099, 1, 1, tzinfo=timezone.utc)))
            stack.enter_context(patch('radar.main.signal.signal'))
            stack.enter_context(patch('radar.main.signal.alarm'))
            for name in ('Gemini', 'FirestoreStore', 'Telegram'):
                stack.enter_context(patch('radar.main.' + name))
            engine = stack.enter_context(patch('radar.main.Engine'))
            engine.return_value.run.return_value = summary, code
            stack.enter_context(patch.dict('os.environ', TELEGRAM_BOT_TOKEN='123:TEST_ONLY'))
            stack.enter_context(redirect_stdout(output))
            result = entrypoint.main()
        return result, json.loads(output.getvalue())

    def test_log_severity_tracks_actual_run_health(self):
        for errors, warnings, code, severity in [([], [], 0, 'INFO'),
                ([], ['mail_not_received_72h:linkedin_email'], 0, 'WARNING'),
                (['source_failed:test'], [], 1, 'ERROR')]:
            with self.subTest(severity=severity):
                result, log = self.invoke({'status': 'partial_failure' if errors else 'success',
                                          'errors': errors, 'warnings': warnings}, code)
                self.assertEqual(result, code)
                self.assertEqual(log['severity'], severity)
                self.assertEqual(log['event'], 'radar_run')
                self.assertEqual(log['warnings'], warnings)
