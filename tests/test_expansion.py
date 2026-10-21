from copy import deepcopy
import json
import unittest
from unittest.mock import patch, MagicMock

from radar.domain import normalize_job, validate_assessment, verified_date
from radar.identity import attach_identity, canonical_url, groups, job_link
from radar.mail_import import discoveries
from radar.sources import retrieve
from radar.public_sources import pageup_listing, pageup_detail, workday_detail, fetch_workday, the_detail
import test_engine as fixtures
from test_engine import SOURCE, BODY


class DedupTests(fixtures.EngineTests):
    # Use the existing state machine fixtures without rerunning inherited tests.
    def test_cross_source_duplicate_analyzed_and_sent_once(self):
        a = deepcopy(self.items[0])
        other = {**SOURCE, "id": "other"}
        b = normalize_job(other, "xyz", a["title"], a["location"], a["url"] + "?utm_source=test", BODY)
        self.engine().run([SOURCE, other], lambda s: ([a if s["id"] == SOURCE["id"] else b], True))
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(len(self.ordinary()), 1)
        self.assertEqual(sum(j.get("duplicate_of") is not None for j in self.store.scan("jobs").values()), 1)

    def test_existing_sent_history_survives_new_official_source(self):
        a = self.items[0]
        self.run_items([a])
        other = {**SOURCE, "id": "official"}
        b = normalize_job(other, "xyz", a["title"], a["location"], a["url"], BODY)
        b["source_quality"] = 10
        self.engine().run([SOURCE, other], lambda s: ([a if s["id"] == SOURCE["id"] else b], True))
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(len(self.ordinary()), 1)

    def test_unknown_delivery_holds_all_aliases(self):
        a = self.items[0]
        self.run_items([a])
        path = next(iter(self.store.scan("notifications")))
        self.store.data[path]["state"] = "unknown"
        other = {**SOURCE, "id": "other"}
        b = normalize_job(other, 2, a["title"], a["location"], a["url"], BODY)
        self.engine().run([other], lambda s: ([b], True))
        self.assertEqual(self.model.calls, 1)
        self.assertTrue(all(j["delivery_hold"] for j in self.store.scan("jobs").values()))

    def test_closed_primary_uses_active_alias_without_initial_resend(self):
        a = self.items[0]
        self.run_items([a])
        other = {**SOURCE, "id": "other"}
        b = normalize_job(other, 2, a["title"], a["location"], a["url"], BODY)
        self.engine().run([SOURCE, other], lambda s: ([], True) if s["id"] == SOURCE["id"] else ([b], True))
        self.assertEqual(len(self.ordinary()), 1)
        self.assertEqual(sum(not j.get("closed") and not j.get("duplicate_of")
                             for j in self.store.scan("jobs").values()), 1)

    def test_same_title_different_requisitions_remain_independent(self):
        self.run_items(self.items[:2])
        self.assertEqual(self.model.calls, 2)

    def test_clear_mismatch_does_not_consume_ai_calls(self):
        job = normalize_job(SOURCE, 30, "Chief People Officer", "Melbourne",
                            "https://test.invalid/30", BODY)
        self.run_items([job])
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.store.get("jobs/" + job["id"])["filter_reason"], "outside_topic")


# Avoid counting the base suite twice; inherited fixtures/helpers remain available.
for _name in list(fixtures.EngineTests.__dict__):
    if _name.startswith("test_") and _name not in DedupTests.__dict__:
        setattr(DedupTests, _name, None)


class AdapterTests(unittest.TestCase):
    source = {"id": "public", "channel": "Academic_AU", "employer": "Test University",
              "allowed_hosts": ["example.edu.au"], "quality": 3}

    def test_ordinal_deadline_requires_the_same_explicit_year(self):
        quote = "Applications Close: Friday 30th of October 2026 at 11:59pm (AEST)."
        self.assertEqual(verified_date("2026-10-30", quote), "2026-10-30")
        with self.assertRaises(ValueError):
            verified_date("2027-10-30", quote)

    def test_location_metadata_repair_does_not_accept_another_city(self):
        job = normalize_job(self.source, "123456", "Research Fellow", "Sydney",
                            "https://example.edu.au/job/123456/test", BODY)
        value = fixtures.assessment()
        value["location"] = {"value": "Sydney", "quote": 'location": "Sydney"'}
        result = validate_assessment(value, job, {"verified_experience": []})
        self.assertEqual(result["location"]["quote"], "Sydney")
        result["location"] = {"value": "London", "quote": 'location": "London"'}
        with self.assertRaises(ValueError):
            validate_assessment(result, job, {"verified_experience": []})

    def test_unicode_slug_is_encoded_without_losing_job_id(self):
        response = MagicMock()
        response.status, response.headers = 200, {}
        response.read.return_value = b"verified body"
        with patch("radar.sources.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value.__enter__.return_value = response
            retrieve("https://example.edu.au/job/123456/physics-xₓ", ["example.edu.au"])
            request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.edu.au/job/123456/physics-x%E2%82%93")

    def test_workday_locale_and_slug_share_requisition_identity(self):
        a = "https://unimelb.wd105.myworkdayjobs.com/en-US/UoM_External_Career/job/Parkville/A_JR-001234"
        b = "https://unimelb.wd105.myworkdayjobs.com/UoM_External_Career/job/Parkville/B_JR-001234"
        self.assertEqual(canonical_url(a), canonical_url(b))
        self.assertNotEqual(canonical_url(a), canonical_url(b.replace("001234", "005678")))
        self.assertTrue(job_link(b.replace("/B_", "/Research-Fellow_")))

    def test_shared_application_guide_does_not_merge_different_jobs(self):
        guide = "https://www.monash.edu/jobs/how-to-apply/applying-for-the-job"
        self.assertFalse(job_link(guide))
        items = {}
        for ref in ("123456", "234567"):
            item = normalize_job(self.source, ref, "Research Fellow", "Melbourne",
                                 "https://example.edu.au/job/" + ref + "/research-fellow", BODY)
            items[item["id"]] = attach_identity(item, [guide])
        self.assertEqual(len(groups(items)), 2)

    def test_pageup_pagination_and_wrong_detail_rejected(self):
        html = b'''<tbody id="search-results-content"><tr><td><a class="job-link"
        href="/en/job/123456/test">Lecturer in Statistics</a></td><td><span class="location">Melbourne</span>
        </td></tr></tbody><a class="more-link" href="/en/listing/?page=2">More Jobs</a>'''
        found, next_page = pageup_listing(self.source, html, "https://example.edu.au/en/listing/")
        self.assertEqual(len(found), 1)
        self.assertTrue(next_page.endswith("?page=2"))
        detail = ('<div id="job-content">Lecturer in Statistics Job No. 123456 ' + BODY + "</div>").encode()
        self.assertTrue(pageup_detail(self.source, found[0], detail)["complete"])
        with self.assertRaises(ValueError):
            pageup_detail(self.source, found[0], detail.replace(b"123456", b"999999"))

    def test_workday_identity_and_country_checks(self):
        candidate = {"title": "Applied Scientist", "bulletFields": ["JR12345"]}
        data = {"jobPostingInfo": {"title": "Applied Scientist", "jobReqId": "JR12345",
                "country": {"descriptor": "Australia"}, "jobDescription": BODY, "location": "Melbourne",
                "externalUrl": "https://example.edu.au/job/test/JR12345", "canApply": True}}
        self.assertTrue(workday_detail(self.source, candidate, json.dumps(data))["complete"])
        data["jobPostingInfo"]["jobReqId"] = "JR99999"
        with self.assertRaises(ValueError):
            workday_detail(self.source, candidate, json.dumps(data))

    def test_workday_zero_total_later_pages_still_checks_full_inventory(self):
        source = {**self.source, "api_base": "https://example.edu.au/cxs/test"}
        seen = []
        def get(url, hosts, timeout, payload=None):
            if payload is not None:
                offset = payload["offset"]
                seen.append(offset)
                numbers = range(offset, min(offset + 20, 21))
                return json.dumps({"total": 21 if offset == 0 else 0,
                    "jobPostings": [{"title": "Applied Scientist", "bulletFields": [f"JR{n:05}"],
                                     "externalPath": f"/job/test/{n}"} for n in numbers]})
            n = int(url.rsplit("/", 1)[1])
            return json.dumps({"jobPostingInfo": {"title": "Applied Scientist", "jobReqId": f"JR{n:05}",
                "jobDescription": BODY, "location": "Melbourne",
                "externalUrl": f"https://example.edu.au/job/test/{n}"}})
        items, complete = fetch_workday(source, get)
        self.assertTrue(complete)
        self.assertEqual(seen, [0, 20])
        self.assertEqual(len(items), 21)
        self.assertTrue(all(i["country"] == "unknown" for i in items))

    def test_query_job_identity_is_not_dropped_as_tracking(self):
        self.assertNotEqual(canonical_url("https://example.edu.au/jobs?ref=123"),
                            canonical_url("https://example.edu.au/jobs?ref=456"))

    def test_pageup_application_id_can_corroborate_missing_visible_number(self):
        candidate = {"id": "123456", "title": "Applied Scientist", "location": "Melbourne",
                     "url": "https://example.edu.au/en/job/123456/test"}
        detail = ('<div id="job-content">Applied Scientist ' + BODY +
                  '</div><a class="apply-link" href="https://example.edu.au/apply?lJobID=123456">Apply</a>')
        self.assertTrue(pageup_detail(self.source, candidate, detail)["complete"])
        with self.assertRaises(ValueError):
            pageup_detail(self.source, candidate, detail.replace("lJobID=123456", "lJobID=999999"))

    def test_pageup_expanded_hr_title_still_requires_matching_job_number(self):
        candidate = {"id": "123456", "title": "Human Resources Business Partner",
                     "location": "Melbourne", "url": "https://example.edu.au/en/job/123456/test"}
        detail = '<div id="job-content">HR Business Partner Job No. 123456 ' + BODY + '</div>'
        self.assertTrue(pageup_detail(self.source, candidate, detail)["complete"])
        with self.assertRaises(ValueError):
            pageup_detail(self.source, candidate, detail.replace("123456", "999999"))

    def test_the_full_jsonld_and_title_validation(self):
        info = {"@type": "JobPosting", "title": "Applied Scientist", "description": BODY,
                "hiringOrganization": {"name": "Test University"}, "identifier": {"value": "123456"}}
        data = '<script type="application/ld+json">' + json.dumps(info) + "</script>"
        candidate = {"id": "567890", "title": "Applied Scientist", "location": "Melbourne",
                     "url": "https://example.edu.au/listing/567890/test/"}
        self.assertTrue(the_detail(self.source, candidate, data)["complete"])
        candidate["title"] = "Another role"
        with self.assertRaises(ValueError):
            the_detail(self.source, candidate, data)

    def test_email_stages_links_without_personal_text_or_analysis(self):
        raw = b"From: private@example.com\r\nSubject: Hi private person\r\nContent-Type: text/plain\r\n\r\nhttps://www.seek.com.au/job/12345678?utm_source=mail"
        items = discoveries(raw)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["complete"])
        self.assertNotIn("private", json.dumps(items))
        self.assertEqual(items[0]["url"], "https://www.seek.com.au/job/12345678")
