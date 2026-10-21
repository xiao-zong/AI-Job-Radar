"""Offline email-to-discovery staging; no mailbox access or automatic job-site fetch."""
import argparse
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .domain import normalize_job
from .identity import attach_identity
from .public_sources import Tree, links


def discoveries(raw):
    if len(raw) > 2_000_000:
        raise ValueError("Email exceeds staging allowance.")
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    urls = set()
    for part in mail.walk():
        if part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        content = part.get_content()
        urls.update(links(Tree(content).root, "https://invalid.invalid/"))
        urls.update(re.findall(r"https://[^\s<>\"']+", content))
    results = []
    for url in sorted(urls):
        p = urlsplit(url)
        if p.username or p.password or p.port not in {None, 443}:
            continue
        match = None
        if p.hostname in {"www.seek.com.au", "seek.com.au", "au.seek.com"}:
            match = re.fullmatch(r"/job/(\d+)/?", p.path)
            source_id = "seek_email"
        elif p.hostname in {"www.linkedin.com", "au.linkedin.com", "linkedin.com"}:
            match = re.fullmatch(r"/jobs/view/(?:[^/]*-)?(\d+)/?", p.path)
            source_id = "linkedin_email"
        if not match:
            continue
        # Store neither email addresses nor the message text. Alerts are discovery
        # links, not verified full job descriptions and cannot enter AI analysis.
        canonical = ("https://www.seek.com.au/job/" if source_id == "seek_email"
                     else "https://www.linkedin.com/jobs/view/") + match[1]
        source = {"id": source_id, "channel": "Industry_MEL"}
        item = normalize_job(source, match[1], "邮件提醒：职位全文待核验", "", canonical, "", complete=False)
        results.append(attach_identity(item))
    return list({j["id"]: j for j in results}.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("eml", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    items = discoveries(args.eml.read_bytes())
    args.output.write_text(json.dumps(items, ensure_ascii=False, indent=2))
    args.output.chmod(0o600)
    print(json.dumps({"discovery_links": len(items), "mailbox_connected": False,
                      "full_descriptions_verified": False}))


if __name__ == "__main__":
    main()
