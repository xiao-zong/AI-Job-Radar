"""Allowlisted retrieval; feeds are discovery records until detail parsing is approved."""
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from scripts.check_sources import (LIMIT, NoRedirect, decode_response, parse_greenhouse,
                                  parse_rss, validate_url)
from .domain import clean_html, normalize_job
from .identity import attach_identity

RSS = "{http://purl.org/rss/1.0/}"
MATHJOBS = "{https://www.mathjobs.org/jobs/ads.rss}"


def retrieve(url, hosts, timeout=20, payload=None):
    validate_url(url, hosts)
    # Public listings can contain Unicode in job slugs; encode the request URI.
    url = urllib.parse.quote(url, safe=":/?=&%+#@")
    headers = {"User-Agent": "AIJobRadar/0.4 (personal daily job discovery)"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, headers=headers,
                                    data=None if payload is None else json.dumps(payload).encode())
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        if response.status != 200 or response.headers.get("x-amzn-waf-action"):
            raise ValueError("Source returned an access challenge or non-success response.")
        return decode_response(response.read(LIMIT + 1), response.headers.get("Content-Encoding", ""))


def mathjobs_candidates(source, data):
    """Use the feed's explicit country, never guess country from employer names."""
    parse_rss(data, source["allowed_hosts"])
    root = ET.fromstring(data)
    if root.tag != "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF":
        raise ValueError("MathJobs feed format changed.")
    candidates, seen = [], set()
    for item in root.findall(RSS + "item"):
        country = item.findtext(MATHJOBS + "Country")
        if not country or not re.fullmatch("[A-Z]{2}", country):
            raise ValueError("MathJobs country metadata missing; coverage needs review.")
        if country != "AU":
            continue
        source_id = item.findtext(MATHJOBS + "ID") or ""
        url = validate_url(item.findtext(RSS + "link"), source["allowed_hosts"])
        parts = urllib.parse.urlsplit(url)
        if not source_id.isdigit() or parts.path != "/jobs/list/" + source_id or source_id in seen:
            raise ValueError("MathJobs identity or duplicate changed.")
        seen.add(source_id)
        candidates.append({"id": source_id, "url": urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, "", "")),
            "title": item.findtext(RSS + "title"), "employer": item.findtext(MATHJOBS + "Univ")})
    # Fail visibly instead of silently truncating the country's feed.
    if len(candidates) > 20:
        raise ValueError("Australian feed exceeds reviewed detail-fetch allowance.")
    return candidates


def parse_mathjobs_detail(source, candidate, data):
    html = data.decode("utf-8")
    main = re.search(r"<main\b[^>]*>(.*?)</main\s*>", html, re.S | re.I)
    if not main:
        raise ValueError("Incomplete MathJobs detail page.")
    main = main.group(1)
    employer = re.search(r"<h2\b[^>]*>(.*?)</h2\s*>", main, re.S | re.I)
    description = re.search(r"""<section\b[^>]*class=["']bld["'][^>]*>(.*?)</section\s*>""",
                            main, re.S | re.I)
    if not employer or not description or len(clean_html(description.group(1))) < 100:
        raise ValueError("MathJobs employer or full description missing.")

    def field(label):
        match = re.search(r"<b>\s*" + re.escape(label) +
                          r"\s*</b>\s*</div>\s*<div\b[^>]*>(.*?)</div\s*>", main, re.S | re.I)
        if not match:
            raise ValueError("MathJobs field missing: " + label)
        return clean_html(match.group(1))

    title, location = field("Position Title:"), field("Position Location:")
    if title != clean_html(candidate["title"]) or not re.search(r"\bAustralia\b", location):
        raise ValueError("MathJobs feed/detail identity or country mismatch.")
    if not re.search(r"\[#" + re.escape(candidate["id"]) + r"(?:,|\])", field("Position ID:")):
        raise ValueError("MathJobs detail ID mismatch.")
    # The full main section includes the labelled deadline and application route.
    # No timezone is inferred from the RSS's EST timestamps.
    location = re.sub(r"\[\s*map\s*\]", "", location).strip()
    from .public_sources import Tree, links
    item = normalize_job({**source, "employer": clean_html(employer.group(1))},
                         candidate["id"], title, location, candidate["url"], main)
    return attach_identity(item, links(Tree(main).root, candidate["url"]))


def fetch_mathjobs(source, data, get=retrieve, clock=time.monotonic):
    started = clock()
    items = []
    for candidate in mathjobs_candidates(source, data):
        remaining = 60 - (clock() - started)
        if remaining < 5:
            raise TimeoutError("Academic detail retrieval exceeded its bounded window.")
        detail = get(candidate["url"], source["allowed_hosts"], timeout=min(10, remaining))
        items.append(parse_mathjobs_detail(source, candidate, detail))
    return items, False  # A discovery feed never proves that omitted vacancies closed.


def fetch(source):
    if source.get("validation_status") != "approved":
        raise ValueError("Production source has not passed manual field/coverage review.")
    if source["kind"] == "icloud_alert":
        from .mail_sources import fetch_icloud
        return fetch_icloud(source)
    from .public_sources import fetch_pageup, fetch_workday, fetch_the, parse_austms
    adapters = {"pageup": fetch_pageup, "workday": fetch_workday, "the": fetch_the}
    if source["kind"] in adapters:
        return adapters[source["kind"]](source, retrieve)
    data = retrieve(source["url"], source["allowed_hosts"])
    if source["kind"] == "austms":
        return parse_austms(source, data)
    if source["kind"] == "mathjobs_rss":
        return fetch_mathjobs(source, data)
    return parse(source, data)


def parse(source, data):
    hosts = source["allowed_hosts"]
    if source["kind"] == "greenhouse":
        parse_greenhouse(data, hosts)
        items = []
        for job in json.loads(data)["jobs"]:
            items.append(normalize_job(source, job["id"], job["title"],
                         job.get("location", {}).get("name", ""), job["absolute_url"], job["content"]))
        return items, True
    if source["kind"] == "rss":
        parse_rss(data, hosts)
        root = ET.fromstring(data)
        ns = "" if root.tag == "rss" else "{http://purl.org/rss/1.0/}"
        elements = root.findall("channel/item") if not ns else root.findall(ns + "item")
        items = []
        for item in elements:
            url = item.findtext(ns + "link")
            items.append(normalize_job(source, url, item.findtext(ns + "title"), "", url,
                                       item.findtext(ns + "description"), complete=False))
        return items, False
    raise ValueError("Unsupported source adapter.")
