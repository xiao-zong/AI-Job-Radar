import unittest

from radar.domain import verified_date
from radar.sources import mathjobs_candidates, parse_mathjobs_detail, fetch_mathjobs

SOURCE = {"id": "mathjobs", "channel": "Academic_AU", "allowed_hosts": ["www.mathjobs.org"]}
FEED = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:m="https://www.mathjobs.org/jobs/ads.rss">
 <channel><title>Test vacancies</title></channel>
 <item><title>Lecturer in Statistics</title><link>https://www.mathjobs.org/jobs/list/123?rss</link>
 <description>Synthetic description.</description><m:Country>AU</m:Country>
 <m:ID>123</m:ID><m:Univ>Test University</m:Univ></item></rdf:RDF>'''
DETAIL = b'''<main><h2>Test University</h2><div class="grid2">
 <div><b>Position ID:</b></div><div>TEST [#123, REQ1]</div>
 <div><b>Position Title:</b></div><div>Lecturer in Statistics</div>
 <div><b>Position Location:</b></div><div>Melbourne, Australia [<a>map</a>]</div>
 <div><b>Appl Deadline:</b></div><div>2026/11/01 23:59:59</div></div>
 <section class="bld">A synthetic teaching and research position requires a doctorate in statistics
 and experience in statistical modelling. Teaching undergraduate courses is part of the role.</section>
 <b>Please apply at the university careers page.</b></main><footer>Navigation</footer>'''


class SourcesTests(unittest.TestCase):
    def test_complete_body_with_explicit_country_and_employer(self):
        candidates = mathjobs_candidates(SOURCE, FEED)
        jobs, inventory = fetch_mathjobs(SOURCE, FEED, get=lambda *a, **k: DETAIL)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(jobs[0]["complete"])
        self.assertEqual(jobs[0]["employer"], "Test University")
        self.assertEqual(jobs[0]["location"], "Melbourne, Australia")
        self.assertIn("2026/11/01", jobs[0]["body"])
        self.assertIn("statistical modelling", jobs[0]["body"])
        self.assertNotIn("Navigation", jobs[0]["body"])
        self.assertFalse(inventory)

    def test_unknown_country_is_not_silently_excluded(self):
        with self.assertRaises(ValueError):
            mathjobs_candidates(SOURCE, FEED.replace(b"<m:Country>AU</m:Country>", b""))
        self.assertEqual(mathjobs_candidates(SOURCE, FEED.replace(b">AU<", b">US<")), [])

    def test_truncated_or_wrong_job_cannot_be_analysed(self):
        candidate = mathjobs_candidates(SOURCE, FEED)[0]
        for detail in (DETAIL.replace(b"</main>", b""), DETAIL.replace(b"[#123,", b"[#999,"),
                       DETAIL.replace(b"class=\"bld\"", b"class=\"other\""),
                       DETAIL.replace(b"Melbourne, Australia", b"London, United Kingdom")):
            with self.assertRaises(ValueError):
                parse_mathjobs_detail(SOURCE, candidate, detail)

    def test_iso_slash_deadline_requires_correct_explicit_year(self):
        self.assertEqual(verified_date("2026-11-01", "2026/11/01 23:59:59"), "2026-11-01")
        with self.assertRaises(ValueError):
            verified_date("2026-11-01", "2027/11/01 23:59:59")
