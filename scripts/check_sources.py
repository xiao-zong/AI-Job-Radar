#!/usr/bin/env python3
"""Read-only endpoint checks. Run in Cloud Shell; no job bodies saved to disk.

Passing means the endpoint responded with plausible data, NOT that the source is
approved, comprehensive, or ready to enable in production.
"""
import gzip
import html
import io
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 8_000_000


def validate_url(value, allowed_hosts):
    if not isinstance(value, str) or len(value) > 600 or any(c.isspace() for c in value):
        raise ValueError("Expected a plain HTTPS URL.")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in allowed_hosts
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("URL is outside the configured HTTPS host allowlist.")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def decode_response(data, encoding=""):
    if len(data) > LIMIT:
        raise ValueError("Response exceeds validation size limit.")
    if encoding.lower() == "gzip" or data.startswith(bytes([31, 139])):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            data = compressed.read(LIMIT + 1)
    elif encoding.lower() not in ("", "identity"):
        raise ValueError("Unsupported content encoding.")
    if len(data) > LIMIT:
        raise ValueError("Decompressed response exceeds validation size limit.")
    return data


def parse_greenhouse(data, hosts):
    payload = json.loads(data)
    jobs = payload.get("jobs")
    total = payload.get("meta", {}).get("total")
    if not isinstance(jobs, list) or type(total) is not int or total != len(jobs):
        raise ValueError("Job list/total is missing or inconsistent; do not call this a complete check.")
    ids = set()
    candidates = []
    for job in jobs:
        if not isinstance(job, dict) or type(job.get("id")) is not int or not job.get("title"):
            raise ValueError("Missing job ID or title.")
        if job["id"] in ids:
            raise ValueError("Duplicate job ID in source response.")
        ids.add(job["id"])
        validate_url(job.get("absolute_url"), hosts)
        body = job.get("content")
        if not isinstance(body, str) or len(html.unescape(body).strip()) < 100:
            raise ValueError("Missing or short description; requires inspection.")
        location = job.get("location", {}).get("name", "")
        title = job["title"]
        if re.search(r"Melbourne", location, re.I) and re.search(r"data|scien|statistic|quant|AI|machine learning", title, re.I):
            candidates.append({"id": job["id"], "title": title, "location": location, "url": job["absolute_url"]})
    return {"count": len(jobs), "reported_total": total, "sample_relevant_titles": candidates[:5],
            "published_at_note": "updated_at is NOT a publication date; leave published_at unknown unless independently supplied."}


def parse_rss(data, hosts):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("DTD/entity declarations are not accepted.")
    root = ET.fromstring(data)
    namespace = ""
    if root.tag == "rss":
        channel = root.find("channel")
        items = [] if channel is None else channel.findall("item")
    elif root.tag == "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF":
        namespace = "{http://purl.org/rss/1.0/}"
        channel = root.find(namespace + "channel")
        items = root.findall(namespace + "item")
    else:
        raise ValueError("Expected RSS 1.0 or 2.0, received a different document format.")
    if channel is None or not channel.findtext(namespace + "title"):
        raise ValueError("Missing RSS channel metadata.")
    for item in items:
        if not item.findtext(namespace + "title") or not item.findtext(namespace + "description"):
            raise ValueError("RSS item has no title or description.")
        validate_url(item.findtext(namespace + "link"), hosts)
    return {"count": len(items), "feed_title": channel.findtext(namespace + "title"),
            "coverage": "Discovery feed only. A feed is not an exhaustive current-vacancy inventory.",
            "next_validation": "Inspect Australian employer/location, detail body and deadlines before enabling."}


def main():
    sources = json.loads((ROOT / "config" / "sources.json").read_text())
    failures = 0
    for source in sources:
        try:
            url = validate_url(source["url"], source["allowed_hosts"])
            request = urllib.request.Request(url, headers={"User-Agent": "AIJobRadar/0.1 (personal low-frequency source validation)"})
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
                data = decode_response(response.read(LIMIT + 1), response.headers.get("Content-Encoding", ""))
            parser = {"greenhouse": parse_greenhouse, "rss": parse_rss, "mathjobs_rss": parse_rss}[source["kind"]]
            report = parser(data, source["allowed_hosts"])
            print(json.dumps({"source": source["id"], "status": "reachable_pending_review", **report}, ensure_ascii=False))
        except urllib.error.HTTPError as error:
            failures += 1
            print(json.dumps({"source": source["id"], "status": "check_failed", "http_status": error.code}))
        except Exception as error:
            failures += 1
            print(json.dumps({"source": source["id"], "status": "check_failed", "reason_type": type(error).__name__}))
    print("No source was enabled. No model calls or Telegram messages were made. No job bodies saved.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
