"""Bounded, public recruitment adapters. No login, redirects or hidden apply routes."""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, parse_qs

from .domain import clean_html, normalize_job
from .identity import attach_identity
from scripts.check_sources import validate_url


class Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def all(self, tag=None, cls=None, id=None):
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if ((tag is None or child.tag == tag)
                        and (cls is None or cls in child.attrs.get("class", "").split())
                        and (id is None or child.attrs.get("id") == id)):
                    found.append(child)
                found.extend(child.all(tag, cls, id))
        return found

    def text(self):
        return " ".join(c.text() if isinstance(c, Node) else c for c in self.children
                        if not isinstance(c, Node) or c.tag not in {"script", "style", "noscript"})

    def one(self, **kwargs):
        found = self.all(**kwargs)
        if len(found) != 1:
            raise ValueError("Expected one recruitment field: " + str(kwargs))
        return found[0]


class Tree(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, data):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(data.decode("utf-8-sig") if isinstance(data, bytes) else data)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def links(node, base):
    return [urljoin(base, n.attrs["href"].strip()) for n in node.all(tag="a") if n.attrs.get("href")]


class Window:
    def __init__(self, source, get, seconds=75):
        self.source, self.get, self.end = source, get, time.monotonic() + seconds

    def read(self, url, payload=None):
        # One retry for transient public reads, within the original source deadline.
        # Workday POST payloads are read-only listing queries. Access challenges,
        # redirects, TLS failures and parser/identity errors are never retried.
        for attempt in range(2):
            left = self.end - time.monotonic()
            if left < 2:
                raise TimeoutError("Source retrieval exceeded its bounded window.")
            try:
                if payload is None:
                    return self.get(url, self.source["allowed_hosts"], timeout=min(12, left))
                return self.get(url, self.source["allowed_hosts"], timeout=min(12, left), payload=payload)
            except (TimeoutError, ConnectionResetError, URLError) as error:
                transient = (isinstance(error, (TimeoutError, ConnectionResetError))
                             or isinstance(error, HTTPError) and error.code in {502, 503, 504}
                             or not isinstance(error, HTTPError)
                             and isinstance(getattr(error, "reason", None), (TimeoutError, ConnectionResetError)))
                if attempt or not transient or self.end - time.monotonic() < 3:
                    raise
                time.sleep(1)


def checked_item(source, source_id, title, location, url, body, urls=(), **extra):
    validate_url(url, source["allowed_hosts"])
    item = normalize_job(source, source_id, title, location, url, body)
    if not item["complete"]:
        raise ValueError("Job detail is incomplete.")
    item.update(extra, source_quality=source.get("quality", 1))
    return attach_identity(item, urls)


def pageup_listing(source, data, base):
    tree = Tree(data).root
    results = tree.one(id="search-results-content")
    jobs = {}
    for anchor in results.all(tag="a", cls="job-link"):
        url = validate_url(urljoin(base, anchor.attrs["href"]), source["allowed_hosts"])
        match = re.search(r"/job/(\d+)/", url)
        if not match or match[1] in jobs:
            raise ValueError("PageUp listing identity changed.")
        row = anchor
        while row.parent and row.tag != "tr":
            row = row.parent
        location = row.one(cls="location").text()
        jobs[match[1]] = {"id": match[1], "title": clean_html(anchor.text()),
                          "location": clean_html(location), "url": url}
    nexts = {urljoin(base, a.attrs["href"]) for a in tree.all(tag="a", cls="more-link")}
    if len(nexts) > 1:
        raise ValueError("Conflicting PageUp pagination.")
    return list(jobs.values()), next(iter(nexts), None)


def pageup_detail(source, candidate, data):
    content = Tree(data).root.one(id="job-content")
    body = content.text()
    def words(value):
        value = re.sub(r"\bhuman resources\b", "hr", value.casefold())
        return set(re.findall(r"\w+", value))
    title_words = words(candidate["title"])
    body_words = words(clean_html(body))
    if not title_words <= body_words:
        raise ValueError("PageUp detail title mismatch: " + candidate["id"] + " " + candidate["title"])
    root = Tree(data).root
    labelled = root.all(id="job")
    identity_text = labelled[0].text() if len(labelled) == 1 else body
    apply_ids = {parse_qs(urlsplit(a.attrs.get("href", "")).query).get("lJobID", [""])[0]
                 for a in root.all(tag="a", cls="apply-link")}
    if (not re.search(r"\b" + re.escape(candidate["id"]) + r"\b", identity_text)
            and apply_ids != {candidate["id"]}):
        raise ValueError("PageUp detail job number missing.")
    return checked_item(source, candidate["id"], candidate["title"], candidate["location"],
                        candidate["url"], body, links(content, candidate["url"]),
                        requisition=candidate["id"])


def fetch_pageup(source, get):
    window, candidates, visited = Window(source, get, seconds=150), [], set()
    url = source["url"]
    while url:
        if url in visited or len(visited) >= 10:
            raise ValueError("PageUp pagination loop or coverage limit.")
        visited.add(url)
        page, url = pageup_listing(source, window.read(url), url)
        candidates.extend(page)
        if len(candidates) > 180:
            raise ValueError("PageUp inventory exceeds reviewed allowance.")
    if len({c["id"] for c in candidates}) != len(candidates):
        raise ValueError("PageUp inventory changed during pagination.")
    def detail(c):
        if re.search(r"\b(scholarships?|studentships?|doctor of philosophy)\b", c["title"], re.I):
            item = normalize_job(source, c["id"], c["title"], c["location"], c["url"], "", complete=False)
            item.update(discovery_skip="student_scholarship", source_quality=source.get("quality", 1))
            return attach_identity(item)
        return pageup_detail(source, c, window.read(c["url"]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        items = list(pool.map(detail, candidates))
    return items, True


def workday_detail(source, candidate, data):
    info = json.loads(data)["jobPostingInfo"]
    if info["title"] != candidate["title"] or info["jobReqId"] not in candidate["bulletFields"]:
        raise ValueError("Workday listing/detail identity mismatch.")
    country = (info.get("country", {}).get("descriptor")
               or info.get("jobRequisitionLocation", {}).get("country", {}).get("descriptor") or "unknown")
    body = info["jobDescription"]
    # Preserve explicit metadata as evidence; never infer a deadline timezone.
    if info.get("endDate"):
        body += "<p>Closing date (site metadata): " + info["endDate"] + "</p>"
    item = checked_item(source, info["jobReqId"], info["title"],
                        info.get("location", "") + ", " + country, info["externalUrl"], body,
                        links(Tree(body).root, info["externalUrl"]), requisition=info["jobReqId"],
                        listing_closed=info.get("canApply") is False, country=country)
    item["published_at"] = info.get("startDate")
    return item


def fetch_workday(source, get):
    window, candidates = Window(source, get), []
    base = source["api_base"]
    expected = None
    for offset in range(0, 180, 20):
        data = json.loads(window.read(base + "/jobs",
                         {"limit": 20, "offset": offset, "searchText": "", "appliedFacets": {}}))
        total = data.get("total")
        # Workday reports the total on offset 0 and returns 0 on later pages.
        if (type(total) is not int or not 0 <= total <= 180
                or (expected is not None and total not in {0, expected})):
            raise ValueError("Workday inventory changed or exceeds reviewed allowance.")
        if expected is None:
            expected = total
        batch = data["jobPostings"]
        if not batch and len(candidates) < expected:
            raise ValueError("Incomplete Workday pagination.")
        candidates.extend(batch)
        if len(candidates) >= expected:
            break
    if len(candidates) != expected or len({c["externalPath"] for c in candidates}) != expected:
        raise ValueError("Workday inventory count mismatch.")
    def detail(c):
        if not c["externalPath"].startswith("/job/") or "?" in c["externalPath"]:
            raise ValueError("Invalid Workday detail path.")
        return workday_detail(source, c, window.read(base + c["externalPath"]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        items = list(pool.map(detail, candidates))
    return items, True


def parse_austms(source, data):
    tree = Tree(data).root
    cards = tree.all(cls="brxe-50eff4")
    if not cards or len(cards) > 100:
        raise ValueError("AustMS recruitment cards missing or limit exceeded.")
    items = []
    for card in cards:
        title = clean_html(card.one(tag="h3").text())
        apply = [a for a in card.all(tag="a") if a.text().strip() == "APPLY"]
        if len(apply) != 1 or " @ " not in title:
            raise ValueError("AustMS title or application route changed.")
        title, employer = title.rsplit(" @ ", 1)
        url = validate_url(apply[0].attrs["href"].strip(), source["allowed_hosts"])
        body = card.text()
        if not card.all(cls="x-read-more_content"):
            raise ValueError("AustMS full description missing.")
        items.append(checked_item({**source, "employer": employer}, url, title, "", url,
                                  body, links(card, source["url"])))
    # Curated list, not an authoritative full vacancy inventory.
    return items, False


def the_listing(source, data, base):
    tree = Tree(data).root
    items = []
    for card in tree.all(tag="li", cls="lister__item"):
        header = card.one(cls="lister__header").one(tag="a")
        url = validate_url(urljoin(base, header.attrs["href"].strip()), source["allowed_hosts"])
        match = re.search(r"/listing/(\d+)/", url)
        if not match or card.attrs.get("id") != "item-" + match[1]:
            raise ValueError("THE listing identity mismatch.")
        items.append({"id": match[1], "url": url.split("?")[0],
                      "title": clean_html(header.text()),
                      "summary": clean_html(" ".join(n.text() for n in card.all(cls="lister__description"))),
                      "employer": clean_html(card.one(cls="lister__meta-item--recruiter").text()),
                      "location": clean_html(card.one(cls="lister__meta-item--location").text())})
    nexts = {urljoin(base, a.attrs["href"].strip()) for a in tree.all(tag="a")
             if a.attrs.get("rel") == "next" or a.attrs.get("aria-label", "").lower() in {"next", "next page"}}
    if len(nexts) > 1 or not items:
        raise ValueError("THE pagination or recruitment list missing.")
    # Derive next page from the explicit numbered navigation when rel=next is absent.
    current = re.search(r"/australia/(\d+)/", base)
    number = int(current[1]) if current else 1
    numbered = {urljoin(base, a.attrs["href"].strip()) for a in tree.all(tag="a")
                if a.attrs.get("aria-label") == "Page " + str(number + 1)}
    return items, next(iter(nexts or numbered), None)


def the_detail(source, candidate, data):
    tree = Tree(data).root
    values = []
    for node in tree.all(tag="script"):
        if node.attrs.get("type") == "application/ld+json":
            value = json.loads("".join(c for c in node.children if isinstance(c, str)))
            values.extend(value if isinstance(value, list) else [value])
    postings = [v for v in values if v.get("@type") == "JobPosting"]
    if len(postings) != 1:
        raise ValueError("THE structured full job detail missing.")
    info = postings[0]
    if clean_html(info["title"]) != candidate["title"]:
        raise ValueError("THE listing/detail title mismatch.")
    employer = info["hiringOrganization"]["name"]
    body = info["description"]
    if info.get("validThrough"):
        body += "<p>Closing timestamp (site metadata): " + info["validThrough"] + "</p>"
    return checked_item({**source, "employer": employer}, candidate["id"], candidate["title"],
                        candidate["location"], candidate["url"], body,
                        links(Tree(body).root, candidate["url"]),
                        requisition=str(info.get("identifier", {}).get("value") or ""))


def fetch_the(source, get):
    window, candidates, visited = Window(source, get), [], set()
    url = source["url"]
    while url:
        if url in visited or len(visited) >= 12:
            raise ValueError("THE pagination loop or coverage allowance exceeded.")
        visited.add(url)
        page, url = the_listing(source, window.read(url), url)
        candidates.extend(page)
        if len(candidates) > 240:
            raise ValueError("THE inventory exceeds reviewed allowance.")
    # Promoted listings can repeat; identity is supplied by THE itself.
    unique = {c["id"]: c for c in candidates}
    def detail(c):
        # THE is an Australia-wide board. Fetch full text for the reviewed topic
        # search; keep other list observations explicitly as unanalysed discovery.
        topic = re.search(r"\b(statistic\w*|probab\w*|mathemat\w*|stochast\w*|data|"
                          r"quant\w*|comput\w*|machine learning|artificial intelligence|"
                          r"ai|analytics|actuar\w*|research fellow|research associate|postdoc\w*)\b",
                          c["title"] + " " + c["summary"], re.I)
        if topic:
            return the_detail(source, c, window.read(c["url"]))
        item = normalize_job({**source, "employer": c["employer"]}, c["id"], c["title"],
                             c["location"], c["url"], c["summary"], complete=False)
        item["discovery_skip"] = "outside_discovery_topics"
        return attach_identity(item)
    with ThreadPoolExecutor(max_workers=2) as pool:
        items = list(pool.map(detail, unique.values()))
    return items, False
