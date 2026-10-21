"""Cloud-only full-source dry run. Does not call Gemini, Firestore or Telegram."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from radar.sources import fetch
from radar.identity import groups, prefilter

ROOT = Path(__file__).resolve().parents[1]


def main():
    current = json.loads((ROOT / "config/sources.json").read_text())
    current = [s for s in current if s.get("enabled")]
    additions = [
        {"id": "austms", "kind": "austms", "url": "https://austms.org.au/resources/austms-careers/",
         "allowed_hosts": ["austms.org.au", "www.unsw.edu.au", "maths.anu.edu.au", "jobs.anu.edu.au",
                           "qut.nga.net.au", "study.uq.edu.au", "unimelb.wd105.myworkdayjobs.com",
                           "careers.pageuppeople.com"], "quality": 2},
        {"id": "unimelb", "kind": "workday", "employer": "University of Melbourne",
         "url": "https://unimelb.wd105.myworkdayjobs.com/en-US/UoM_External_Career",
         "api_base": "https://unimelb.wd105.myworkdayjobs.com/wday/cxs/unimelb/UoM_External_Career",
         "allowed_hosts": ["unimelb.wd105.myworkdayjobs.com"], "quality": 3},
        {"id": "rmit", "kind": "workday", "employer": "RMIT University",
         "url": "https://rmit.wd3.myworkdayjobs.com/RMIT_Careers",
         "api_base": "https://rmit.wd3.myworkdayjobs.com/wday/cxs/rmit/RMIT_Careers",
         "allowed_hosts": ["rmit.wd3.myworkdayjobs.com"], "quality": 3},
        {"id": "monash", "kind": "pageup", "employer": "Monash University",
         "url": "https://careers.pageuppeople.com/513/cw/en/listing",
         "allowed_hosts": ["careers.pageuppeople.com"], "quality": 3},
        {"id": "latrobe", "kind": "pageup", "employer": "La Trobe University",
         "url": "https://jobs.latrobe.edu.au/en/listing/",
         "allowed_hosts": ["jobs.latrobe.edu.au"], "quality": 3},
        {"id": "deakin", "kind": "pageup", "employer": "Deakin University",
         "url": "https://careers.deakin.edu.au/en/listing/",
         "allowed_hosts": ["careers.deakin.edu.au"], "quality": 3},
        {"id": "the_australia", "kind": "the",
         "url": "https://www.timeshighereducation.com/unijobs/listings/australia/",
         "allowed_hosts": ["www.timeshighereducation.com"], "quality": 1},
    ]
    for source in additions:
        source.update(channel="Academic_AU", enabled=True, validation_status="approved")
        if source["id"] in {"latrobe", "deakin"}:
            source.update(enabled=False, validation_status="blocked_access_challenge")
    sources = current + [s for s in additions if s["id"] not in {v["id"] for v in current}]
    out = ROOT / "work/expansion"
    out.mkdir(exist_ok=True)
    results, items_by_source = [], {}
    def check(source):
        started = time.monotonic()
        try:
            items, complete = fetch(source)
            (out / (source["id"] + "-jobs.json")).write_text(json.dumps(items))
            return source, items, {"id": source["id"], "status": "ok", "count": len(items),
                    "complete_inventory": complete, "seconds": round(time.monotonic() - started, 1),
                    "samples": [{"title": j["title"], "employer": j["employer"],
                                 "body_chars": len(j["body"]), "url": j["url"]} for j in items[:2]]}
        except Exception as error:
            return source, [], {"id": source["id"], "status": "failed", "error": str(error)[:300],
                               "seconds": round(time.monotonic() - started, 1)}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for source, items, result in pool.map(check, [s for s in sources if s["enabled"]]):
            results.append(result)
            items_by_source[source["id"]] = items
            print(json.dumps(result), flush=True)
    all_jobs = {j["id"]: j for items in items_by_source.values() for j in items}
    grouped = groups(all_jobs)
    summary = {"sources": results, "records": len(all_jobs), "unique": len(grouped),
               "duplicate_records": len(all_jobs) - len(grouped),
               "prefiltered": sum(bool(prefilter(j)) for j in all_jobs.values()),
               "duplicate_examples": [[{"source": all_jobs[k]["source"], "title": all_jobs[k]["title"]}
                                       for k in g] for g in grouped if len(g) > 1][:8]}
    (out / "validation.json").write_text(json.dumps(summary, indent=2))
    (out / "sources.proposed.json").write_text(json.dumps(sources, indent=2))
    print("SUMMARY", json.dumps({k: v for k, v in summary.items() if k != "sources"}), flush=True)
    if any(r["status"] != "ok" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
