"""Conservative cross-source identity. Titles alone never establish identity."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from .domain import compact, digest

EMPLOYERS = {
    "australian national university anu": "australian national university",
    "the australian national university": "australian national university",
    "the university of melbourne": "university of melbourne",
    "university of new south wales": "unsw",
    "unsw sydney": "unsw",
}


def employer_key(name):
    key = compact(re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()))
    return EMPLOYERS.get(key, key)


def canonical_url(url):
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    path = parts.path.rstrip("/")
    # Workday locale and presentation slug vary; tenant + requisition are stable.
    req = re.search(r"_(JR-?\d+)$", path, re.I)
    if parts.hostname.endswith(".myworkdayjobs.com") and "/job/" in path and req:
        return "workday:" + parts.hostname + ":" + req[1].upper()
    pageup = re.search(r"/job/(\d+)(?:/|$)", path)
    if pageup and (parts.hostname in {"careers.pageuppeople.com", "jobs.latrobe.edu.au", "careers.deakin.edu.au"}):
        tenant = path.split("/")[1] if parts.hostname == "careers.pageuppeople.com" else parts.hostname
        tenant = {"533": "jobs.latrobe.edu.au"}.get(tenant, tenant)
        return "pageup:" + tenant + ":" + pageup[1]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.casefold().startswith("utm_")
             and k.casefold() not in {"rss", "linksource", "gh_src"}]
    return urlunsplit(("https", parts.hostname, path, urlencode(sorted(query)), ""))


def job_link(url):
    parts = urlsplit(url)
    # Exclude homepages, broad career pages, login, search and apply redirects.
    specific = bool(re.search(r"/(?:jobs?/(?:list/)?|listing/)\d+(?:/[^/]+)?/?$", parts.path))
    specific |= bool(parts.hostname and parts.hostname.endswith(".myworkdayjobs.com")
                     and "/job/" in parts.path and re.search(r"_JR-?\d+/?$", parts.path, re.I))
    specific |= parts.hostname == "jobs.anu.edu.au" and bool(re.fullmatch(r"/jobs/[^/]+/?", parts.path))
    return (parts.scheme == "https" and not parts.query and specific
            and not re.search(r"/(?:login|search|external-redirect[^/]*)(?:/|$)", parts.path, re.I))


def attach_identity(item, urls=()):
    primary = canonical_url(item["url"])
    keys = {"url:" + primary} if primary else set()
    for url in urls:
        if job_link(url):
            canonical = canonical_url(url)
            if canonical:
                keys.add("url:" + canonical)
    employer = employer_key(item.get("employer"))
    ref = item.get("requisition", "")
    if employer and re.fullmatch(r"(?:JR-?)?\d{4,12}", ref, re.I):
        # Require a corroborating title as an additional check across ATS vendors.
        title = compact(re.sub(r"[^a-z0-9]+", " ", item["title"].casefold()))
        keys.add("req:" + digest([employer, ref.upper(), title]))
    item["identity_keys"] = sorted(keys)
    return item


def groups(jobs):
    parents = {path: path for path in jobs}
    def root(p):
        while parents[p] != p:
            parents[p] = parents[parents[p]]
            p = parents[p]
        return p
    seen = {}
    for path, job in jobs.items():
        keys = set(job.get("identity_keys", []))
        canonical = canonical_url(job["url"])
        if canonical:
            keys.add("url:" + canonical)
        for key in keys:
            if key in seen:
                parents[root(path)] = root(seen[key])
            seen[key] = path
    result = {}
    for path in jobs:
        result.setdefault(root(path), []).append(path)
    return list(result.values())


def prefilter(job):
    """Only deterministic exclusions; all records remain inspectable."""
    if job.get("discovery_skip"):
        return job["discovery_skip"]
    title = job["title"].casefold()
    if job.get("country") and job["country"] not in {"Australia", "unknown"}:
        return "outside_australia"
    if re.search(r"\b(phd scholarship|phd candidates?|phd research opportunity|studentship)\b", title):
        return "student_scholarship"
    positive = re.search(r"\b(data|statistic\w*|probab\w*|stochast\w*|quant\w*|mathemat\w*|"
                         r"machine learning|artificial intelligence|ai|applied scient\w*|"
                         r"comput\w*|optim\w*|actuar\w*|analytics|econometric\w*)\b", title)
    if positive:
        return None
    clear_mismatch = re.search(
        r"\b(account (?:manager|executive)|workplace|people officer|human resources|"
        r"reception|counsellor|animal technologist|campaign|marketing|security operations|"
        r"architecture|law|nursing|physiotherapy|paramedicine|surgery|pedagogy|"
        r"print operations|stores purchasing|student recruitment|mechanical specialist)\b", title)
    return "outside_topic" if clear_mismatch else None
