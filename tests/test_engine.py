from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

from radar.domain import digest, normalize_job, validate_assessment, reminder_window
from radar.engine import Engine, LeaseLost
from radar.main import config_check
from radar.store import MemoryStore
from radar.telegram import DeliveryUnknown

ROOT = Path(__file__).resolve().parents[1]
SOURCE = {"id": "test", "kind": "greenhouse", "channel": "Industry_MEL",
          "employer": "Test employer", "enabled": True, "validation_status": "approved"}
BODY = ("Melbourne, Australia. We require statistical modelling experience. Python is preferred. "
        "Full working rights in Australia are required. This is a permanent research position.")


def assessment():
    return {"decision": "review", "priority": 2, "summary_zh": "资历需核对。",
            "location": {"value": "Melbourne", "quote": "Melbourne, Australia"},
            "work_authorization": {"value": "Full working rights", "quote": "Full working rights in Australia are required."},
            "salary": {"value": "unknown", "quote": ""}, "remote": {"value": "unknown", "quote": ""},
            "deadline": {"date": None, "timezone": None, "quote": ""},
            "requirements": [{"label": "Statistical modelling", "level": "required",
                              "quote": "We require statistical modelling experience.", "gap": True,
                              "candidate_evidence_ids": []}], "transferable_skills": []}


class Model:
    model_id = "gemini-test"
    def __init__(self):
        self.calls = 0
    def analyze(self, job, profile):
        self.calls += 1
        return assessment(), {"totalTokenCount": 10}


class Sender:
    def __init__(self, error=None):
        self.sent, self.error = [], error
    def send(self, target, text):
        self.sent.append((target, text))
        if self.error:
            raise self.error
        return len(self.sent)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.store, self.model, self.sender = MemoryStore(), Model(), Sender()
        self.profile = json.loads((ROOT / "config/profile.example.json").read_text())
        self.timestamp = datetime(2026, 10, 20, 0, tzinfo=timezone.utc).timestamp()
        self.items = [normalize_job(SOURCE, n, "Applied Scientist", "Melbourne", f"https://test.invalid/{n}", BODY)
                      for n in range(8)]

    def engine(self):
        return Engine(self.store, self.model, self.sender, -1001, self.profile, now=lambda: self.timestamp)

    def run_items(self, items=None):
        return self.engine().run([SOURCE], lambda s: (self.items if items is None else items, True))

    def ordinary(self):
        return [e for e in self.store.scan("notifications").values() if e["kind"] == "ordinary"]

    def test_repeat_run_uses_cache_and_daily_quota_not_per_run(self):
        self.run_items()
        self.assertEqual(self.model.calls, 8)
        self.assertEqual(len(self.sender.sent), 5)
        self.run_items()
        self.assertEqual(self.model.calls, 8)
        self.assertEqual(len(self.sender.sent), 5)
        self.timestamp += 86400
        self.run_items()
        self.assertEqual(len(self.sender.sent), 8)
        self.assertTrue(all(e["event_type"] == "baseline" for e in self.ordinary()))

    def test_daily_model_limit_preserves_backlog_across_restarts(self):
        self.profile["model_calls_per_day_limit"] = 2
        result, _ = self.run_items()
        self.assertEqual(result["backlog_count"], 6)
        self.run_items()
        self.assertEqual(self.model.calls, 2)
        self.timestamp += 86400
        self.run_items()
        self.assertEqual(self.model.calls, 4)

    def test_rejected_response_is_retained_without_an_extra_call_or_recommendation(self):
        class InvalidModel(Model):
            def analyze(self, job, profile):
                value, usage = super().analyze(job, profile)
                value["requirements"][0]["quote"] = "An unsupported requirement not in the original."
                return value, usage
        self.model = InvalidModel()
        _, code = self.run_items(self.items[:1])
        self.assertEqual(code, 1)
        saved = self.store.get("jobs/" + self.items[0]["id"])
        self.assertEqual(saved["validation_error"], "Claim has no matching source quotation.")
        self.assertIn("unsupported", saved["validation_response"]["requirements"][0]["quote"])
        self.assertEqual(saved["usage"]["totalTokenCount"], 10)
        self.assertEqual(saved["state"], "pending")
        self.run_items(self.items[:1])
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.ordinary(), [])

    def test_preference_works_with_distinct_ingestion_times_without_starvation(self):
        self.profile["model_calls_per_day_limit"] = 1
        self.profile["model_calls_per_run_limit"] = 1
        jobs = [
            normalize_job(SOURCE, 90, "Applied Scientist", "Sydney", "https://test.invalid/90", BODY),
            normalize_job(SOURCE, 91, "Applied Scientist", "Melbourne", "https://test.invalid/91", BODY),
        ]
        engine = self.engine()
        self.assertTrue(engine.acquire())
        engine.ingest(SOURCE, jobs, True)
        first = self.store.get("jobs/" + jobs[0]["id"])
        first["queued_at"] -= 5
        engine.save("jobs/" + jobs[0]["id"], first)
        engine.recover()
        engine.evaluate()
        self.assertEqual(self.store.get("jobs/" + jobs[1]["id"])["state"], "evaluated")
        engine.release()
        self.timestamp += 86400
        self.run_items(jobs)
        self.assertEqual(self.store.get("jobs/" + jobs[0]["id"])["state"], "evaluated")

    def test_authorization_is_separate_without_weakening_professional_evidence(self):
        value = assessment()
        value["requirements"].append({
            "label": "Full working rights in Australia", "level": "required",
            "quote": "Full working rights in Australia are required.",
            "gap": False, "candidate_evidence_ids": []})
        checked = validate_assessment(value, self.items[0], self.profile)
        self.assertEqual(len(checked["requirements"]), 1)
        self.assertEqual(checked["work_authorization"]["value"], "Full working rights")
        checked["requirements"][0]["gap"] = False
        with self.assertRaises(ValueError):
            validate_assessment(checked, self.items[0], self.profile)

    def test_newly_seen_does_not_claim_new_publication(self):
        self.run_items(self.items[:1])
        self.run_items(self.items[:2])
        self.assertIn("新发现（不等于新发布）", self.sender.sent[-1][1])

    def test_source_failure_does_not_close_existing_jobs(self):
        self.run_items(self.items[:1])
        def fail(source):
            raise TimeoutError()
        result, code = self.engine().run([SOURCE], fail)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "partial_failure")
        self.assertFalse(next(iter(self.store.scan("jobs").values()))["closed"])

    def test_incomplete_feed_items_never_call_model(self):
        self.items[0]["complete"] = False
        self.run_items(self.items[:1])
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(next(iter(self.store.scan("jobs").values()))["state"], "review_required")

    def test_content_change_reanalyzes_without_duplicate_material_notification(self):
        self.run_items(self.items[:1])
        new = normalize_job(SOURCE, 0, "Applied Scientist", "Melbourne", "https://test.invalid/0", BODY + " Equal opportunity employer.")
        self.run_items([new])
        self.assertEqual(self.model.calls, 2)
        self.assertEqual(len(self.sender.sent), 1)

    def test_profile_change_invalidates_cache(self):
        self.run_items(self.items[:1])
        self.profile["interests"] = ["Probability"]
        self.run_items(self.items[:1])
        self.assertEqual(self.model.calls, 2)
        self.assertEqual(len(self.sender.sent), 1)

    def test_uncertain_send_is_never_automatically_retried(self):
        self.sender.error = DeliveryUnknown("uncertain")
        self.run_items(self.items[:1])
        self.sender.error = None
        self.run_items(self.items[:1])
        self.assertEqual(self.ordinary()[0]["state"], "unknown")
        ordinary_messages = [t for _, t in self.sender.sent if "初始清单" in t]
        self.assertEqual(len(ordinary_messages), 1)

    def test_overlap_and_stale_owner_cannot_write_or_release_new_lease(self):
        old, other = self.engine(), self.engine()
        self.assertTrue(old.acquire())
        self.assertFalse(other.acquire())
        self.timestamp += 121
        self.assertTrue(other.acquire())
        with self.assertRaises(LeaseLost):
            old.save("test/value", {"bad": True})
        old.release()
        self.assertEqual(self.store.get("control/lease")["owner"], other.owner)
        self.assertGreater(self.store.get("control/lease")["expires"], self.timestamp)

    def test_interrupted_send_recovers_to_unknown(self):
        self.run_items(self.items[:1])
        path, event = next(iter(self.store.scan("notifications").items()))
        self.store.data[path]["state"] = "sending"
        self.run_items(self.items[:1])
        self.assertEqual(self.store.get(path)["state"], "unknown")

    def test_invented_evidence_and_unsupported_dates_are_rejected(self):
        value = assessment()
        value["requirements"][0]["candidate_evidence_ids"] = ["invented"]
        with self.assertRaises(ValueError):
            validate_assessment(value, self.items[0], self.profile)
        value = assessment()
        value["deadline"] = {"date": "2026-10-21", "quote": "We require statistical modelling experience.", "timezone": None}
        with self.assertRaises(ValueError):
            validate_assessment(value, self.items[0], self.profile)

    def test_two_day_window_wins_and_unknown_timezone_never_reminds(self):
        deadline = {"date": "2026-10-22", "timezone": "Australia/Melbourne"}
        self.assertEqual(reminder_window(deadline, self.timestamp), 2)
        deadline["timezone"] = None
        self.assertIsNone(reminder_window(deadline, self.timestamp))

    def test_activation_gate_is_closed_by_default(self):
        config = json.loads((ROOT / "config/deployment.example.json").read_text())
        with self.assertRaises(ValueError):
            config_check(config, self.profile, [SOURCE])

    def test_crash_after_delivery_before_commit_does_not_resend(self):
        original = self.store.atomic
        failed = [False]
        def interrupted(paths, mutate):
            if self.sender.sent and any(p.startswith("notifications/") for p in paths) and not failed[0]:
                failed[0] = True
                raise TimeoutError("simulated post-delivery database outage")
            return original(paths, mutate)
        self.store.atomic = interrupted
        with self.assertRaises(TimeoutError):
            self.run_items(self.items[:1])
        self.store.atomic = original
        self.run_items(self.items[:1])
        self.assertEqual(self.ordinary()[0]["state"], "unknown")
        self.assertEqual(sum("初始清单" in t for _, t in self.sender.sent), 1)

    def test_quota_commit_failure_never_starts_model_call(self):
        original = self.store.atomic
        def interrupted(paths, mutate):
            if any(p.startswith("daily/") for p in paths):
                raise TimeoutError("quota commit outcome unknown")
            return original(paths, mutate)
        self.store.atomic = interrupted
        with self.assertRaises(TimeoutError):
            self.run_items(self.items[:1])
        self.assertEqual(self.model.calls, 0)

    def test_superseded_initial_event_can_be_replaced_after_reassessment(self):
        self.run_items(self.items[:1])
        path, event = next(iter(self.store.scan("notifications").items()))
        self.store.data[path]["state"] = "superseded"
        job_path = next(iter(self.store.scan("jobs")))
        self.store.data[job_path].pop("last_sent_material")
        self.run_items(self.items[:1])
        self.assertEqual(self.store.get(path)["state"], "sent")

    def test_expired_safety_stop_rejected_before_cloud_clients(self):
        config = json.loads((ROOT / "config/deployment.example.json").read_text())
        config.update({k: True for k in config if k.endswith("verified") or k in
                       {"activation_authorized", "source_samples_reviewed"}})
        config["chat_id"] = -1001
        with self.assertRaises(ValueError):
            config_check(config, self.profile, [SOURCE], datetime(2027, 1, 16, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
