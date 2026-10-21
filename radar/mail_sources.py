"""Official alert cards only: bounded redirects, no authenticated job-site scrape."""
from email import policy
from email.parser import BytesParser
import html
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener

from scripts.check_sources import NoRedirect
from .domain import compact, digest, normalize_job
from .icloud_reader import ICloudReader, MailAccessError, sender_is_alert
from .identity import attach_identity
from .public_sources import Tree

DIRECT_HOSTS = {
    "seek_email": {"www.seek.com.au", "seek.com.au", "au.seek.com"},
    "linkedin_email": {"www.linkedin.com", "linkedin.com", "au.linkedin.com"},
}
FOOTER = re.compile(
    r"unsubscribe|manage (?:your )?(?:job )?alerts|privacy policy|"
    r"this email was intended|you are receiving|you received this email", re.I)


def direct_job(url, source_id):
    try:
        p = urlsplit(html.unescape(url))
        if (p.scheme != "https" or p.hostname not in DIRECT_HOSTS[source_id]
                or p.username or p.password or p.port not in {None, 443}):
            return None
        expression = (r"/job/([0-9]{5,15})/?" if source_id == "seek_email"
                      else r"/(?:comm/)?jobs/view/(?:[^/]*-)?([0-9]{5,15})/?")
        match = re.fullmatch(expression, p.path)
        if not match:
            return None
        base = ("https://www.seek.com.au/job/" if source_id == "seek_email"
                else "https://www.linkedin.com/jobs/view/")
        return match[1], base + match[1]
    except (ValueError, KeyError):
        return None


class Resolver:
    """Follow SEEK's observed email redirect path, never arbitrary links or pixels."""
    def __init__(self, seconds=45, opener=None):
        self.end = time.monotonic() + seconds
        self.opener = opener or build_opener(NoRedirect())
        self.cache, self.requests = {}, 0

    def resolve(self, url, source_id):
        if url in self.cache:
            return self.cache[url]
        original = url
        for _ in range(4):
            direct = direct_job(url, source_id)
            if direct:
                self.cache[original] = direct
                return direct
            p = urlsplit(url)
            if (source_id != "seek_email" or p.scheme != "https"
                    or p.hostname != "email.s.seek.com.au"
                    or not p.path.startswith("/uni/ss/c/") or p.username or p.password
                    or p.port not in {None, 443}):
                self.cache[original] = None
                return None
            left = self.end - time.monotonic()
            if left < 2 or self.requests >= 80:
                raise MailAccessError("Alert link resolution allowance exceeded.")
            self.requests += 1
            try:
                response = self.opener.open(
                    Request(url, method="GET", headers={"User-Agent": "AIJobRadar/0.5"}),
                    timeout=min(6, left))
            except HTTPError as error:
                response = error
            except Exception:
                raise MailAccessError("Alert link resolution failed.") from None
            try:
                # Do not read a tracking response body, accept cookies or follow
                # arbitrary destinations. The direct job URL is never fetched.
                code = response.getcode()
                target = response.headers.get("Location")
                if code not in {301, 302, 303, 307, 308} or not target:
                    raise MailAccessError("Alert redirect format requires review.")
                url = urljoin(url, target)
            finally:
                response.close()
        raise MailAccessError("Alert redirect chain exceeds its allowance.")


def chunks(node):
    """Text only; ignore images, styles, links' query strings and hidden markup."""
    result = []
    if node.tag in {"style", "script", "noscript", "head"}:
        return result
    if (node.attrs.get("aria-hidden") == "true"
            or re.search(r"display\s*:\s*none", node.attrs.get("style", ""), re.I)):
        return result
    for child in node.children:
        if isinstance(child, str):
            value = compact(child)
            if value:
                result.append(value)
        else:
            result.extend(chunks(child))
    return result


def safe_text(value):
    # Only selected card text can leave the mail parser. Recipient addresses,
    # opaque tracking links and the whole message never enter storage or AI.
    value = re.sub(r"https?://\S+", "", value)
    value = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "", value, flags=re.I)
    return compact(value)


def parse_alert(raw, source, received_at, resolver=None):
    if len(raw) > 2_000_000:
        raise MailAccessError("Alert exceeds size allowance.")
    mail = BytesParser(policy=policy.default).parsebytes(raw)
    sid = source["id"]
    if not sender_is_alert(mail, sid):
        raise MailAccessError("Unexpected alert sender.")
    resolver = resolver or Resolver()
    items = {}
    for part in mail.walk():
        if (part.get_content_disposition() == "attachment"
                or part.get_content_type() != "text/html"):
            continue
        root = Tree(part.get_content()).root
        anchors = root.all(tag="a")
        mapped = {}
        for anchor in anchors:
            href = anchor.attrs.get("href", "").strip()
            text = " ".join(chunks(anchor))
            if not text or FOOTER.search(text):
                continue
            # Tracking links are resolved only for text-bearing candidates.
            identity = resolver.resolve(href, sid)
            if identity:
                mapped[id(anchor)] = identity
        for anchor in anchors:
            identity = mapped.get(id(anchor))
            if not identity:
                continue
            labels = chunks(anchor)
            title = next((safe_text(t) for t in labels if len(t) >= 5), "")
            if (not title or len(title) > 200
                    or re.fullmatch(r"(?:view|see|apply|learn).{0,20}", title, re.I)):
                continue
            card, cursor = anchor, anchor
            # Expand inside a single job card, stopping at any second job or
            # email footer. No entire email is submitted as job evidence.
            for _ in range(7):
                parent = cursor.parent
                if parent is None or parent.tag in {"body", "html", ""}:
                    break
                text = " ".join(chunks(parent))
                ids = {mapped[id(a)][0] for a in parent.all(tag="a") if id(a) in mapped}
                if ids - {identity[0]} or len(text) > 2000 or FOOTER.search(text):
                    break
                if parent.tag in {"td", "tr", "table", "div", "li"}:
                    card = parent
                cursor = parent
            text = safe_text(" ".join(chunks(card)))
            if len(text) < len(title) or len(text) > 2000:
                raise MailAccessError("Alert card boundaries require review.")
            # Do not infer employer, salary, requirements or location from the
            # user's saved search. Unknown metadata stays unknown.
            match = re.search(
                r"\bMelbourne(?:\s*,?\s*(?:Victoria|VIC))?(?:\s*,?\s*Australia)?\b", text, re.I)
            location = match[0] if match else ""
            item = normalize_job(source, identity[0], title, location, identity[1],
                                 html.escape(text), complete=False)
            item.update(evidence_kind="mail_excerpt", mail_parser_version="1",
                        evidence_received_at=received_at, source_quality=1)
            item["content_hash"] = digest([item["content_hash"], "mail_excerpt_v1"])
            item = attach_identity(item)
            old = items.get(item["id"])
            if old is None or len(item["body"]) > len(old["body"]):
                items[item["id"]] = item
    if not items:
        # An empty/unrecognized message is visible as a source failure instead
        # of silently claiming that all subscribed jobs were processed.
        raise MailAccessError("No recognizable job cards; alert template needs review.")
    return list(items.values())


def fetch_icloud(source):
    path = os.environ.get("RADAR_ICLOUD_FILE")
    if not path:
        raise MailAccessError("iCloud credential mount is missing.")
    reader = ICloudReader(path)
    messages = reader.messages(source["id"], source["folder"], source["not_before"])
    resolver = Resolver()
    items = {}
    for message in messages:
        for item in parse_alert(message["raw"], source, message["received_at"], resolver):
            # Newest message wins; older repeated recommendations cannot
            # refresh timestamps or replace its newer evidence.
            items.setdefault(item["id"], item)
    return list(items.values()), False
