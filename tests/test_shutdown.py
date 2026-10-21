from datetime import datetime, timezone
import unittest
from unittest.mock import Mock

from radar.shutdown import run


class ShutdownTests(unittest.TestCase):
    def test_cannot_pause_early(self):
        session = Mock()
        with self.assertRaises(ValueError):
            run(session, datetime(2026, 10, 20, tzinfo=timezone.utc))
        session.get.assert_not_called()
        session.post.assert_not_called()

    def test_access_check_has_no_mutation(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"state": "ENABLED"}
        run(session, check_only=True)
        self.assertEqual(session.get.call_count, 2)
        session.post.assert_not_called()

    def test_stops_exactly_its_two_schedules(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {"state": "ENABLED"}
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {"state": "PAUSED"}
        result = run(session, datetime(2027, 1, 14, tzinfo=timezone.utc))
        self.assertEqual(set(result), {"radar-daily", "radar-credit-stop"})
        self.assertEqual(set(result.values()), {"PAUSED"})
        self.assertEqual(session.post.call_count, 2)
