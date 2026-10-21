"""Deterministic identity, evidence checks, deadlines and plaintext messages."""
from datetime import date, datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
import re
from zoneinfo import ZoneInfo

PARSER_VERSION = "3"
PROMPT_VERSION = "2"
SCHEMA_VERSION = "1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def compact(value):
    return " ".join(value.split())


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        elif tag in {"p", "br", "li", "div", "h1", "h2", "h3", "td"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def clean_html(value):
    parser = TextParser()
    parser.feed(html.unescape(value))
    return compact("".join(parser.parts))


def normalize_job(source, source_id, title, location, url, body, complete=True):
    body = clean_html(body)
    if len(title) > 250 or len(location) > 250:
        raise ValueError("Unexpectedly long title or location; manual source review required.")
    if not title or len(body) < 100:
        complete = False
    if len(body) > 50000:
        raise ValueError("Description exceeds full-input limit; do not silently truncate.")
    item = {"source": source["id"], "source_id": str(source_id),
            "channel": source["channel"], "employer": source.get("employer"),
            "title": compact(title), "location": compact(location), "url": url,
            "body": body, "complete": complete, "published_at": None,
            "parser_version": PARSER_VERSION}
    item["id"] = digest([source["id"], str(source_id)])[:32]
    item["content_hash"] = digest({k: item[k] for k in
                                 ("title", "location", "body", "url", "employer", "complete")})
    return item


def cache_key(job, profile, model):
    return digest([job["id"], job["content_hash"], PARSER_VERSION, digest(profile),
                   PROMPT_VERSION, SCHEMA_VERSION, model])


def analysis_ready(job):
    return bool(job.get("complete") or (
        job.get("evidence_kind") == "mail_excerpt"
        and job.get("source") in {"seek_email", "linkedin_email"}
        and job.get("mail_parser_version") == "1"
        and job.get("evidence_received_at", 0) > 0
        and len(job.get("body", "")) >= 30 and job.get("title")))


def rank_hint(job):
    title = job["title"].lower()
    topic = bool(re.search(r"data|statistic|probabil|stochastic|quant|scientist|machine learning|applied ai", title))
    melbourne = bool(re.search(r"\bMelbourne\b", job.get("location", ""), re.I))
    return 2 * int(melbourne) + 3 * int(topic)


def verified_date(value, quote):
    if value is None:
        return None
    parsed = date.fromisoformat(value)
    # Accept only a date whose explicit components can be found in its evidence.
    variants = [parsed.isoformat(), parsed.strftime("%Y/%m/%d"),
                parsed.strftime("%d/%m/%Y"),
                f"{parsed.day} {parsed.strftime('%B')} {parsed.year}",
                f"{parsed.day} {parsed.strftime('%b')} {parsed.year}",
                f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"]
    normalized_quote = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)(?:\s+of)?\s+",
                              r"\1 ", compact(quote), flags=re.I)
    if not any(v.casefold() in normalized_quote.casefold() for v in variants):
        raise ValueError("Deadline date is not corroborated by its quote.")
    return value


def validate_assessment(value, job, profile):
    if not isinstance(value, dict) or value.get("decision") not in {"review", "pass"}:
        raise ValueError("Invalid assessment decision.")
    if type(value.get("priority")) is not int or value["priority"] not in range(4):
        raise ValueError("Invalid priority.")
    if not isinstance(value.get("summary_zh"), str) or not 1 <= len(value["summary_zh"]) <= 500:
        raise ValueError("Invalid summary.")
    evidence_ids = {e["evidence_id"] for e in profile["verified_experience"]}
    text = compact(job["title"] + " " + job["location"] + " " + job["body"]).casefold()

    def quote_ok(quote):
        if not isinstance(quote, str) or not 8 <= len(quote) <= 600 or compact(quote).casefold() not in text:
            raise ValueError("Claim has no matching source quotation.")

    for name in ("location", "work_authorization", "salary", "remote"):
        fact = value.get(name)
        if not isinstance(fact, dict) or not isinstance(fact.get("value"), str):
            raise ValueError("Missing fact field.")
        if fact["value"] == "unknown":
            fact["quote"] = ""
        else:
            if len(fact["value"]) > 250:
                raise ValueError("Fact is too long.")
            if (name == "location" and job.get("location")
                    and compact(fact["value"]).casefold() == compact(job["location"]).casefold()
                    and (not isinstance(fact.get("quote"), str)
                         or len(fact["quote"]) < 8 or compact(fact["quote"]).casefold() not in text)):
                # The location field itself is source evidence, including short
                # names such as Sydney. Never accept a different claimed location.
                fact["quote"] = job["location"]
                continue
            quote_ok(fact.get("quote"))
    deadline = value.get("deadline")
    if not isinstance(deadline, dict):
        raise ValueError("Missing deadline field.")
    if deadline.get("date"):
        quote_ok(deadline.get("quote"))
        verified_date(deadline["date"], deadline["quote"])
    else:
        deadline.update(date=None, quote="", timezone=None)
    # An unverified timezone must never trigger a deadline reminder.
    if deadline.get("timezone"):
        ZoneInfo(deadline["timezone"])
        if deadline["timezone"] not in deadline.get("quote", ""):
            deadline["timezone"] = None
    requirements = value.get("requirements")
    if not isinstance(requirements, list) or len(requirements) > 12:
        raise ValueError("Invalid requirements.")
    professional_requirements = []
    for req in requirements:
        quote_ok(req.get("quote"))
        if (req.get("level") not in {"required", "preferred"}
                or type(req.get("gap")) is not bool
                or not isinstance(req.get("label"), str) or not 1 <= len(req["label"]) <= 80):
            raise ValueError("Invalid requirement.")
        ids = req.get("candidate_evidence_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) and i in evidence_ids for i in ids):
            raise ValueError("Unverified candidate evidence reference.")
        # Work authorization has its own fact field. An exact duplicate with a
        # narrowly recognized authorization label is not a professional skill,
        # and removing it makes no claim about the candidate's qualifications.
        authorization_label = re.fullmatch(
            r"(?:full )?(?:australian )?(?:work|working) rights(?: in australia)?"
            r"|(?:australian )?work authori[sz]ation", req["label"], re.I)
        if (authorization_label
                and compact(req["quote"]).casefold() == compact(value["work_authorization"]["quote"]).casefold()):
            continue
        if not req["gap"] and not ids:
            raise ValueError("A claimed match must reference verified experience.")
        professional_requirements.append(req)
    value["requirements"] = professional_requirements
    requirements = professional_requirements
    transfer = value.get("transferable_skills")
    if not isinstance(transfer, list) or len(transfer) > 4:
        raise ValueError("Invalid transferable skills.")
    for skill in transfer:
        quote_ok(skill.get("requirement_quote"))
        if skill.get("candidate_evidence_id") not in evidence_ids:
            raise ValueError("Transfer claim references unverified experience.")
        for key in ("explanation", "limitation"):
            if not isinstance(skill.get(key), str) or not 1 <= len(skill[key]) <= 300:
                raise ValueError("Transfer claim requires explanation and limitation.")
    # Material hash uses stable source quotations, not a paraphrased model score.
    material = {name: compact(value[name]["quote"]).casefold()
                for name in ("location", "work_authorization", "salary", "remote")}
    material["deadline"] = deadline
    material["requirements"] = sorted((r["level"], compact(r["quote"]).casefold()) for r in requirements)
    value["material_hash"] = digest(material)
    return value


def reminder_window(deadline, now):
    if not deadline.get("date") or not deadline.get("timezone"):
        return None
    days = (date.fromisoformat(deadline["date"]) -
            datetime.fromtimestamp(now, ZoneInfo(deadline["timezone"])).date()).days
    return 2 if 0 <= days <= 2 else 7 if 2 < days <= 7 else None


def clip(text, units=3500):
    raw = text.encode("utf-16-le")
    return text if len(raw) <= units * 2 else raw[:(units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def render(job, assessment, event_type, event_id, status):
    labels = {"baseline": "初始清单", "new": "新发现（不等于新发布）", "change": "关键条件变化",
              "deadline2": "2 天内截止", "deadline7": "7 天内截止"}
    deadline = assessment["deadline"].get("date") or "原文未说明"
    gaps = [r["label"] + ("（必要）" if r["level"] == "required" else "（加分）")
            for r in assessment["requirements"] if r["gap"]]
    evidence = [r["quote"] for r in assessment["requirements"]][:2]
    checked = datetime.fromtimestamp(job["last_verified"], timezone.utc).isoformat(timespec="minutes")
    # Keep the canonical link and event identifier before variable-length prose.
    sources = "、".join(sorted({p["source"] for p in job.get("provenance", [])})) or job["source"]
    excerpt = job.get("evidence_kind") == "mail_excerpt"
    basis = "仅基于邮件摘要；完整要求及是否仍在招聘待核验。\n" if excerpt else ""
    return clip(f"{labels[event_type]} · {job['channel']}\n{job['title']}\n"
                f"雇主：{job.get('employer') or '见原文'}\n地点：{job['location'] or '需确认'}\n"
                f"{job['url']}\n事件：{event_id[:12]}\n"
                f"发现来源：{sources}\n{basis}"
                f"截止：{deadline}；时区：{assessment['deadline'].get('timezone') or '需确认'}\n"
                f"工作许可：{assessment['work_authorization']['value']}\n"
                f"AI 待审核摘要：{assessment['summary_zh']}\n"
                f"待核对要求：{'；'.join(gaps) or '请按原文核对'}\n"
                f"原文依据：{' / '.join(evidence) or '见官方链接'}\n"
                f"{'邮件收到' if excerpt else '核验'}：{checked}\n{status}")
