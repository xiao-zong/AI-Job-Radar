from datetime import datetime, timezone
from email.message import EmailMessage
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from radar.domain import analysis_ready, cache_key, render
from radar.engine import Engine
from radar.icloud_reader import MailAccessError
from radar.mail_sources import Resolver, direct_job, fetch_icloud, parse_alert
from radar.model import Gemini
from radar.store import MemoryStore

SOURCE = {"id": "linkedin_email", "channel": "Industry_MEL", "kind": "icloud_alert"}
RECEIVED = datetime(2026, 10, 20, 4, tzinfo=timezone.utc).timestamp()
HTML = """<html><body><p>Hello private.person@example.org</p><table>
<tr><td><a href="https://www.linkedin.com/comm/jobs/view/data-scientist-123456789?trackingId=private">
Data Scientist</a><p>Example Research</p><p>Melbourne, Victoria, Australia</p>
<p>Statistical modelling with Python.</p></td></tr>
<tr><td><a href="https://www.linkedin.com/jobs/view/234567891?trackingId=private">
Quantitative Analyst</a><p>Example Finance</p><p>Sydney, Australia</p></td></tr>
</table><p>Unsubscribe private.person@example.org</p></body></html>"""


def raw_mail(content=HTML):
    message = EmailMessage()
    message["From"] = "jobs-noreply@linkedin.com"
    message["Subject"] = "Your job alert"
    message.set_content("Open the HTML alert.")
    message.add_alternative(content, subtype="html")
    return message.as_bytes()


def assessment():
    value = {"decision": "review", "priority": 2, "summary_zh": "完整要求待核验。",
             "deadline": {"date": None, "timezone": None, "quote": ""},
             "requirements": [], "transferable_skills": []}
    for key in ["location", "work_authorization", "salary", "remote"]:
        value[key] = {"value": "unknown", "quote": ""}
    return value


class Model:
    model_id = "gemini-test"
    def __init__(self):
        self.calls = []
    def analyze(self, job, profile):
        self.calls.append(job["id"])
        return assessment(), {"totalTokenCount": 10}


class Sender:
    def __init__(self):
        self.sent = []
    def send(self, target, text):
        self.sent.append(text)
        return len(self.sent)


class AlertTests(unittest.TestCase):
    def test_cards_do_not_mix_jobs_or_capture_recipient_or_tracking_token(self):
        jobs = parse_alert(raw_mail(), SOURCE, RECEIVED)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["title"], "Data Scientist")
        self.assertEqual(jobs[0]["location"], "Melbourne, Victoria, Australia")
        self.assertNotIn("Quantitative", jobs[0]["body"])
        self.assertNotIn("Melbourne", jobs[1]["body"])
        self.assertNotIn("private", json.dumps(jobs))
        self.assertTrue(all(not j["complete"] and analysis_ready(j) for j in jobs))
        self.assertEqual(jobs[0]["url"], "https://www.linkedin.com/jobs/view/123456789")
        self.assertIsNone(jobs[0]["employer"])
        self.assertEqual(jobs[1]["location"], "")

    def test_unrecognized_template_fails_visibly(self):
        with self.assertRaises(MailAccessError):
            parse_alert(raw_mail("<p>Your account password was changed.</p>"), SOURCE, RECEIVED)

    def test_arbitrary_external_urls_are_not_requested(self):
        opener = MagicMock()
        resolver = Resolver(opener=opener)
        for url in ["https://attacker.invalid/job/123456789",
                    "https://email.s.seek.com.au/unsubscribe/private",
                    "http://email.s.seek.com.au/uni/ss/c/private",
                    "https://email.s.seek.com.au@attacker.invalid/uni/ss/c/private"]:
            self.assertIsNone(resolver.resolve(url, "seek_email"))
        opener.open.assert_not_called()
        self.assertIsNone(direct_job("https://www.linkedin.com:bad/jobs/view/123456789", "linkedin_email"))

    def test_seek_redirect_canonicalizes_without_fetching_job_or_body(self):
        url = "https://email.s.seek.com.au/uni/ss/c/private"
        opener = MagicMock()
        response = MagicMock()
        response.getcode.return_value = 302
        response.headers = {"Location": "https://www.seek.com.au/job/123456789?tracking=private"}
        opener.open.return_value = response
        resolver = Resolver(opener=opener)
        self.assertEqual(resolver.resolve(url, "seek_email"),
                         ("123456789", "https://www.seek.com.au/job/123456789"))
        self.assertEqual(resolver.resolve(url, "seek_email")[0], "123456789")
        opener.open.assert_called_once()
        response.read.assert_not_called()
        response.close.assert_called_once()

    def test_redirect_to_other_host_is_not_followed(self):
        opener = MagicMock()
        response = MagicMock()
        response.getcode.return_value = 302
        response.headers = {"Location": "https://attacker.invalid/job/123456789"}
        opener.open.return_value = response
        self.assertIsNone(Resolver(opener=opener).resolve(
            "https://email.s.seek.com.au/uni/ss/c/private", "seek_email"))
        opener.open.assert_called_once()

    def test_no_tracking_url_in_failure_message(self):
        opener = MagicMock()
        opener.open.side_effect = RuntimeError("https://email.s.seek.com.au/uni/ss/c/private")
        with self.assertRaises(MailAccessError) as error:
            Resolver(opener=opener).resolve(
                "https://email.s.seek.com.au/uni/ss/c/private", "seek_email")
        self.assertNotIn("private", str(error.exception))

    def test_dedup_cache_timestamp_and_single_delivery_across_polls(self):
        jobs = parse_alert(raw_mail(), SOURCE, RECEIVED)
        store, model, sender = MemoryStore(), Model(), Sender()
        profile = json.loads((Path(__file__).resolve().parents[1] /
                              "config/profile.example.json").read_text())
        now = [RECEIVED + 100]
        source = {**SOURCE, "enabled": True, "validation_status": "approved"}
        def run():
            return Engine(store, model, sender, -1001, profile,
                          now=lambda: now[0]).run([source], lambda _: (jobs, False))
        run()
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(sender.sent), 2)
        self.assertIn("仅基于邮件摘要", sender.sent[0])
        self.assertIn("邮件收到", sender.sent[0])
        now[0] += 86400
        run()
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(sender.sent), 2)
        self.assertEqual(store.get("jobs/" + jobs[0]["id"])["last_verified"], RECEIVED)
        now[0] += 86400
        engine = Engine(store, model, sender, -1001, profile, now=lambda: now[0])
        self.assertFalse(engine.eligible(store.get("jobs/" + jobs[0]["id"])))

    def test_newest_email_is_used_and_omitted_jobs_not_closed(self):
        source = {**SOURCE, "folder": "AI Job Radar", "not_before": RECEIVED - 100}
        with patch.dict("os.environ", {"RADAR_ICLOUD_FILE": "/secrets/test"}), \
                patch("radar.mail_sources.ICloudReader") as reader:
            reader.return_value.messages.return_value = [
                {"raw": raw_mail(), "received_at": RECEIVED},
                {"raw": raw_mail(), "received_at": RECEIVED - 50}]
            jobs, complete = fetch_icloud(source, cache=MemoryStore())
        self.assertEqual(len(jobs), 2)
        self.assertFalse(complete)
        self.assertTrue(all(j["evidence_received_at"] == RECEIVED for j in jobs))

    def test_missing_initial_alert_becomes_visible_after_72_hours(self):
        profile = json.loads((Path(__file__).resolve().parents[1] /
                              "config/profile.example.json").read_text())
        source = {**SOURCE, "enabled": True, "not_before": RECEIVED}
        store = MemoryStore()
        engine = Engine(store, Model(), Sender(), -1001, profile,
                        now=lambda: RECEIVED + 73 * 3600)
        summary, code = engine.run([source], lambda _: ([], False))
        self.assertEqual(code, 0)
        self.assertIn("mail_not_received_72h:linkedin_email", summary["warnings"])
        self.assertEqual(summary["errors"], [])
        self.assertTrue(store.get("sources/linkedin_email")["first_alert_pending"])

    def test_mail_only_prompt_does_not_change_public_job_prompt(self):
        session = MagicMock()
        response = session.post.return_value
        response.status_code = 200
        response.content = b"ok"
        response.json.return_value = {"candidates": [{"finishReason": "STOP",
            "content": {"parts": [{"text": "{}"}]}}]}
        model = Gemini("project-example", "global", "gemini-test", session=session)
        job = parse_alert(raw_mail(), SOURCE, RECEIVED)[0]
        profile = {k: [] for k in ["verified_experience", "learning_or_exposure",
                                   "interests", "work_authorization", "channels"]}
        model.analyze(job, profile)
        body = session.post.call_args.kwargs["json"]
        self.assertIn("NOT a full job description", body["systemInstruction"]["parts"][0]["text"])
        self.assertNotIn("@", body["contents"][0]["parts"][0]["text"])
        job.pop("evidence_kind")
        model.analyze(job, profile)
        self.assertNotIn("NOT a full job description",
                         session.post.call_args.kwargs["json"]["systemInstruction"]["parts"][0]["text"])


if __name__ == "__main__":
    unittest.main()
