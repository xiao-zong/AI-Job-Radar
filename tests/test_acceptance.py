from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from radar.acceptance import check_config, reserve, run
from radar.domain import normalize_job
from radar.model import Gemini
from radar.store import MemoryStore

ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config/deployment.example.json").read_text())
        self.profile = json.loads((ROOT / "config/profile.example.json").read_text())
        self.store = MemoryStore()

    def test_unapproved_acceptance_never_touches_storage(self):
        with patch.object(self.store, "atomic") as atomic:
            with self.assertRaises(ValueError):
                run("firestore", self.config, self.profile, self.store)
            atomic.assert_not_called()

    def test_acceptance_does_not_require_or_set_production_activation(self):
        self.config.update(acceptance_authorized=True, credit_coverage_verified=True,
                           cost_controls_verified=True, stop_before_utc="2099-01-01T00:00:00Z")
        result = run("firestore", self.config, self.profile, self.store)
        self.assertEqual(result["roundtrip"], "verified")
        self.assertFalse(self.config["activation_authorized"])
        self.assertEqual(next(iter(self.store.scan("acceptance").values()))["status"], "passed")

    def test_unknown_attempt_uses_allowance_and_cannot_be_unlimited(self):
        reserve(self.store, "telegram")
        with self.assertRaises(ValueError):
            reserve(self.store, "telegram")
        reserve(self.store, "model")
        reserve(self.store, "model")
        with self.assertRaises(ValueError):
            reserve(self.store, "model")

    def test_acceptance_rejects_expiry_and_wrong_project(self):
        self.config.update(acceptance_authorized=True, credit_coverage_verified=True,
                           cost_controls_verified=True)
        with self.assertRaises(ValueError):
            check_config(self.config, datetime(2027, 1, 16, tzinfo=timezone.utc))
        self.config["project_id"] = "unrelated-project"
        with self.assertRaises(ValueError):
            check_config(self.config, datetime(2026, 10, 20, tzinfo=timezone.utc))

    def test_replay_validates_saved_response_without_calling_model(self):
        from test_engine import assessment
        self.config.update(acceptance_authorized=True, credit_coverage_verified=True,
                           cost_controls_verified=True, stop_before_utc="2099-01-01T00:00:00Z",
                           model_replay_record="a" * 32)
        value = assessment()
        value["requirements"][0]["quote"] = "requires experience in statistical modelling."
        value["requirements"].append({
            "label": "Full working rights in Australia", "level": "required",
            "quote": "Full working rights in Australia are required.", "gap": False,
            "candidate_evidence_ids": []})
        value["location"]["quote"] = "Melbourne, Australia"
        record = {"operation": "model", "status": "validation_failed",
                  "model_response": value, "usage": {"totalTokenCount": 100}}
        self.store.atomic(["acceptance/" + "a" * 32],
                          lambda data: data.update({"acceptance/" + "a" * 32: record}))
        with patch("radar.acceptance.Gemini") as model:
            result = run("model_replay", self.config, self.profile, self.store)
            model.assert_not_called()
        self.assertFalse(result["paid_model_call"])
        self.assertEqual(result["schema_and_evidence"], "verified")


class ModelBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / "config/profile.example.json").read_text())
        self.job = normalize_job({"id": "test", "channel": "Industry_MEL"}, 1,
                                 "Applied Scientist", "Melbourne", "https://example.invalid/job",
                                 "Statistical modelling experience is required. " * 5)

    def test_oversized_utf8_payload_does_not_make_http_request(self):
        from unittest.mock import Mock
        session = Mock()
        self.job["body"] = "量" * 30000
        model = Gemini("your-gcp-project-id", "global", "gemini-3.8-flash", session)
        with self.assertRaises(ValueError):
            model.analyze(self.job, self.profile)
        session.post.assert_not_called()

    def test_no_retry_when_generation_is_truncated(self):
        from unittest.mock import Mock
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.content = b"{}"
        session.post.return_value.json.return_value = {"candidates": [{"finishReason": "MAX_TOKENS"}]}
        model = Gemini("your-gcp-project-id", "global", "gemini-3.8-flash", session)
        with self.assertRaises(RuntimeError):
            model.analyze(self.job, self.profile)
        session.post.assert_called_once()
        options = session.post.call_args.kwargs
        self.assertEqual(options["json"]["generationConfig"]["thinkingConfig"], {"thinkingLevel": "LOW"})
        self.assertEqual(options["json"]["generationConfig"]["maxOutputTokens"], 4096)
        self.assertFalse(options["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
