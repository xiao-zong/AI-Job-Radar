"""Persistent queue and fenced outbox. No network operation lives in a transaction."""
from datetime import datetime, date
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time
import random
import uuid
from zoneinfo import ZoneInfo

from .domain import (analysis_ready, cache_key, digest, rank_hint, reminder_window,
                     render, clip, validate_assessment)
from .telegram import TelegramError, DeliveryUnknown
from .model import ModelServiceError
from .identity import groups, prefilter


class LeaseLost(RuntimeError):
    pass


def assessment_exhausted(job):
    # Keep all requests in attempts; provider failures do not imply bad job evidence.
    failures = job.get("service_failures", 0)
    return job.get("attempts", 0) - failures >= 2 or failures >= 4


class Engine:
    def __init__(self, store, model, telegram, chat_id, profile, now=time.time,
                 monotonic=time.monotonic, soft_seconds=450):
        self.store, self.model, self.telegram = store, model, telegram
        self.chat_id, self.profile = chat_id, profile
        self.now, self.monotonic = now, monotonic
        self.stop = monotonic() + soft_seconds
        self.owner = uuid.uuid4().hex
        self.generation = None
        self.calls, self.errors, self.warnings = 0, [], []
        self.jobs = {}

    def time_left(self, margin=0):
        return self.monotonic() + margin < self.stop

    def day(self):
        return datetime.fromtimestamp(self.now(), ZoneInfo("Australia/Melbourne")).date().isoformat()

    def acquire(self):
        def take(values):
            old = values["control/lease"] or {}
            if old.get("expires", 0) > self.now():
                return None
            generation = old.get("generation", 0) + 1
            values["control/lease"] = {"owner": self.owner, "generation": generation,
                                       "expires": self.now() + 120}
            return generation
        self.generation = self.store.atomic(["control/lease"], take)
        return self.generation is not None

    def atomic(self, paths, mutate):
        paths = list(dict.fromkeys(["control/lease", *paths]))

        def guarded(values):
            lock = values["control/lease"] or {}
            if (lock.get("owner") != self.owner or lock.get("generation") != self.generation
                    or lock.get("expires", 0) <= self.now()):
                raise LeaseLost("Run no longer owns the lease.")
            result = mutate(values)
            lock["expires"] = self.now() + 120
            return result
        return self.store.atomic(paths, guarded)

    def release(self):
        def finish(values):
            lock = values["control/lease"] or {}
            if lock.get("owner") == self.owner and lock.get("generation") == self.generation:
                lock["expires"] = 0
        self.store.atomic(["control/lease"], finish)

    def save(self, path, value):
        self.atomic([path], lambda data: data.__setitem__(path, value))

    def recover(self):
        self.jobs = self.store.scan("jobs")
        for path, job in self.jobs.items():
            if job["state"] == "evaluating":
                job["state"] = "review_required" if assessment_exhausted(job) else "pending"
                job["error"] = "interrupted_evaluation"
                self.save(path, job)
        for path, event in self.store.scan("notifications").items():
            if event["state"] == "sending":
                event.update(state="unknown", error="interrupted_delivery")
                self.save(path, event)

    def ingest(self, source, items, complete_inventory):
        source_path = "sources/" + source["id"]
        previous = self.store.get(source_path) or {}
        baseline = not previous.get("initialized", False)
        seen = set()
        for item in items:
            if not self.time_left(20):
                self.errors.append("ingestion_time_limit:" + source["id"])
                return  # Never mark omitted jobs closed after partial ingestion.
            path = "jobs/" + item["id"]
            seen.add(path)
            old = self.jobs.get(path, {})
            key = cache_key(item, self.profile, self.model.model_id)
            changed = key != old.get("cache_key")
            job = {**old, **item, "first_seen": old.get("first_seen", self.now()),
                   "baseline": old.get("baseline", baseline), "closed": item.get("listing_closed", False),
                   "discovery_skip": item.get("discovery_skip"),
                   "last_verified": (min(self.now(), item["evidence_received_at"])
                                     if item.get("evidence_kind") == "mail_excerpt" else self.now()),
                   "cache_key": key}
            if changed:
                job.update(state="pending" if analysis_ready(item) else "review_required",
                           attempts=0, service_failures=0, queued_at=self.now(), retry_after=0)
            self.save(path, job)
            self.jobs[path] = job
        if complete_inventory:
            for path, job in self.jobs.items():
                if job["source"] == source["id"] and path not in seen and not job.get("closed"):
                    job["closed"] = True
                    self.save(path, job)
        source_state = {"initialized": True, "last_success": self.now(),
                        "count": len(items), "status": "ok",
                        "complete_inventory": complete_inventory}
        source_warnings = list(dict.fromkeys(getattr(items, "health_warnings", [])))
        if source_warnings:
            source_state.update(status="partial", warnings=source_warnings)
            self.warnings.extend("source_warning:" + source["id"] + ":" + code for code in source_warnings)
        if source.get("kind") == "icloud_alert":
            last = max([previous.get("last_alert_received_at", 0),
                        getattr(items, "last_alert_received_at", 0)]
                       + [j.get("evidence_received_at", 0) for j in items])
            source_state["last_alert_received_at"] = last
            source_state["first_alert_pending"] = not bool(last)
            if self.now() - (last or source.get("not_before", self.now())) > 72 * 3600:
                source_state["status"] = "partial" if source_warnings else "no_recent_alert"
                # A healthy mailbox can contain no new matching alerts, including
                # over weekends. Silence is a coverage warning, not a failed read.
                self.warnings.append("mail_not_received_72h:" + source["id"])
        self.save(source_path, source_state)

    def source_failed(self, source_id, error):
        path = "sources/" + source_id
        old = self.store.get(path) or {}
        self.save(path, {**old, "status": "failed", "last_failure": self.now(),
                         "error_type": type(error).__name__})
        self.errors.append("source_failed:" + source_id)

    def model_succeeded(self):
        # Record each actual validated assessment immediately. A later provider
        # failure in the same run must not erase this independent success signal.
        self.save("control/model_service", {"last_success": self.now(),
                  "first_failure": None, "retry_after": 0})

    def model_failed(self, error, retry_after):
        health = self.store.get("control/model_service") or {}
        first_failure = health.get("first_failure")
        if not first_failure:
            # Older deployments stored only the most recent failure timestamp.
            # Retain that available outage evidence when no success was recorded.
            first_failure = (health.get("last_failure") if not health.get("last_success") else None) or self.now()
        health.update(code=error.code, retryable=error.retryable,
                      first_failure=first_failure, last_failure=self.now(),
                      retry_after=retry_after)
        self.save("control/model_service", health)
        anchor = health.get("last_success") or first_failure
        target = self.errors if not error.retryable or self.now() - anchor >= 24 * 3600 else self.warnings
        target.append("model_service_failed:" + error.code)

    def deduplicate(self):
        """Keep source observations and delivery history; select one analysis target."""
        events = list(self.store.scan("notifications").values())
        uncertain = {e.get("job_id") for e in events if e["state"] in {"unknown", "sending"}}
        for paths in groups(self.jobs):
            members = [self.jobs[p] for p in paths]
            # Existing confirmed delivery/assessment wins when still active.
            winner = min(members, key=lambda j: (
                bool(j.get("closed")), not bool(j.get("last_sent_material")),
                j.get("state") != "evaluated", not j.get("complete"), -j.get("source_quality", 1),
                j["first_seen"], j["id"]))
            inherited = next((j.get("last_sent_material") for j in members if j.get("last_sent_material")), None)
            hold = any(j["id"] in uncertain for j in members)
            provenance = [{"source": j["source"], "url": j["url"],
                           "last_verified": j["last_verified"], "closed": bool(j.get("closed"))}
                          for j in sorted(members, key=lambda j: j["id"])]
            for path in paths:
                job = self.jobs[path]
                job["duplicate_of"] = None if job is winner else winner["id"]
                job["delivery_hold"] = hold
                if job is winner:
                    job["provenance"] = provenance
                    if inherited and not job.get("last_sent_material"):
                        job["last_sent_material"] = inherited
                reason = prefilter(job)
                job["filter_reason"] = reason
                if reason and job["state"] in {"pending", "review_required"}:
                    job["state"] = "filtered"
                elif not reason and job["state"] == "filtered":
                    job["state"] = "pending" if analysis_ready(job) else "review_required"
                self.save(path, job)

    def evaluate(self):
        pending = [(p, j) for p, j in self.jobs.items() if j["state"] == "pending"
                   and not j.get("closed") and not j.get("duplicate_of") and not j.get("delivery_hold")]
        # Within a waiting-day cohort, prioritize location/topic. Individual ingest
        # timestamps otherwise prevent relevance from ever breaking a tie.
        # Older cohorts still win, so new relevant work cannot starve old work.
        pending.sort(key=lambda pair: (-int((self.now() - pair[1]["queued_at"]) // 86400),
                                      -rank_hint(pair[1]), pair[1]["queued_at"], pair[0]))
        # Daily alert excerpts expire quickly. Reserve up to three early slots
        # so a large initial public inventory cannot starve them past 48 hours.
        alerts = [pair for pair in pending if pair[1].get("evidence_kind") == "mail_excerpt"
                  and self.now() - pair[1]["last_verified"] <= 48 * 3600]
        promoted = {p for p, _ in alerts[:3]}
        pending = alerts[:3] + [pair for pair in pending if pair[0] not in promoted]
        cooldown = self.store.get("control/model_service") or {}
        if cooldown.get("retry_after", 0) > self.now():
            self.warnings.append("model_service_cooldown:" + cooldown.get("code", "unknown"))
            return
        for path, job in pending:
            if not self.time_left(95) or self.calls >= self.profile["model_calls_per_run_limit"]:
                break
            if job.get("retry_after", 0) > self.now() or self.now() - job["last_verified"] > 48 * 3600:
                continue
            if assessment_exhausted(job):
                job["state"] = "review_required"
                self.save(path, job)
                continue
            quota_path = "daily/" + self.day()
            provider_failure, budget_stop = None, False
            # Every HTTP attempt has its own durable quota reservation. A retry
            # consumes the same 10/run and 30/day allowance as a new assessment.
            for request_index in range(2):
                if not self.time_left(95) or self.calls >= self.profile["model_calls_per_run_limit"]:
                    break
                def reserve(values):
                    quota = values[quota_path] or {"model_calls": 0, "ordinary": 0}
                    if quota["model_calls"] >= self.profile["model_calls_per_day_limit"]:
                        return False
                    current = values[path]
                    if current["state"] != "pending" or current["cache_key"] != job["cache_key"]:
                        return False
                    quota["model_calls"] += 1
                    current.update(state="evaluating", attempts=current.get("attempts", 0) + 1)
                    values[quota_path] = quota
                    return True
                if not self.atomic([path, quota_path], reserve):
                    budget_stop = True
                    break
                self.calls += 1
                job["attempts"] = job.get("attempts", 0) + 1
                job["validation_response"] = None
                try:
                    output, usage = self.model.analyze(job, self.profile)
                    job.update(validation_response=deepcopy(output), usage=usage)
                    result = validate_assessment(output, job, self.profile)
                except ModelServiceError as error:
                    provider_failure = error
                    failures = job.get("service_failures", 0) + 1
                    job["service_failures"] = failures
                    delay = max(error.retry_after, min(900 * 2 ** min(failures - 1, 3), 7200))
                    job.update(state="review_required" if assessment_exhausted(job) else "pending",
                               error="ModelServiceError", validation_error=error.code,
                               model_http_status=error.http_status,
                               retry_after=self.now() + (delay if error.retryable else 86400))
                    self.save(path, job)
                    if (error.retryable and request_index == 0 and job["state"] == "pending"
                            and error.retry_after <= 5 and self.time_left(101)
                            and self.calls < self.profile["model_calls_per_run_limit"]):
                        # Bounded backoff with jitter. Longer Retry-After values
                        # defer to a later scheduled run instead of blocking here.
                        time.sleep(max(error.retry_after, min(2 ** failures, 5)) + random.random())
                        continue
                    break
                except Exception as error:
                    # Provider recovery followed by bad evidence is a validation
                    # failure, never a reason to relax the evidence checks.
                    provider_failure = None
                    job.update(state="review_required" if assessment_exhausted(job) else "pending",
                               error=type(error).__name__, validation_error=str(error)[:300],
                               retry_after=self.now() + 86400)
                    # A rejected individual assessment is safely withheld. Only
                    # expected validator ValueErrors are non-operational; JSON
                    # decoding, runtime and unexpected exceptions remain errors.
                    target = self.warnings if type(error) is ValueError else self.errors
                    target.append("assessment_failed:" + job["id"])
                    self.save(path, job)
                    break
                job.update(state="evaluated", assessment=result, assessed_key=job["cache_key"],
                           assessed_at=self.now(), usage=usage, error=None, model_http_status=None,
                           retry_after=0, validation_response=None, validation_error=None)
                self.save(path, job)
                self.model_succeeded()
                provider_failure = None
                break
            if provider_failure:
                # Leave remaining jobs queued. An isolated retryable failure is
                # a warning; an outage without actual AI success for 24 hours or
                # a non-retryable service/configuration error remains a failure.
                self.model_failed(provider_failure, job["retry_after"])
                break
            if budget_stop:
                break

    def eligible(self, job):
        if (job.get("closed") or job.get("duplicate_of") or job.get("delivery_hold")
                or job.get("filter_reason") or job["state"] != "evaluated"
                or job.get("assessed_key") != job["cache_key"]
                or self.now() - job["last_verified"] > 48 * 3600
                or job["assessment"]["decision"] != "review"):
            return False
        deadline = job["assessment"]["deadline"]
        if deadline.get("date"):
            tz = ZoneInfo(deadline.get("timezone") or "Australia/Melbourne")
            if date.fromisoformat(deadline["date"]) < datetime.fromtimestamp(self.now(), tz).date():
                return False
        return True

    def enqueue(self, event_id, value):
        path = "notifications/" + event_id

        def create(values):
            old = values[path]
            if old and old["state"] not in {"pending", "superseded"}:
                return
            values[path] = {**value, "id": event_id, "state": "pending",
                            "created": old["created"] if old else self.now()}
        self.atomic([path], create)

    def prepare_notifications(self):
        events = self.store.scan("notifications")
        uncertain = {e.get("job_id") for e in events.values() if e["state"] == "unknown"}
        for job in self.jobs.values():
            if not self.eligible(job) or job["id"] in uncertain:
                continue
            assessment = job["assessment"]
            event_type = None
            if not job.get("last_sent_material"):
                event_type = "baseline" if job["baseline"] else "new"
                event_id = digest([job["id"], "initial"])
            elif assessment["material_hash"] != job["last_sent_material"]:
                event_type = "change"
                event_id = digest([job["id"], "change", assessment["material_hash"]])
            if event_type:
                self.enqueue(event_id, {"kind": "ordinary", "event_type": event_type,
                                       "job_id": job["id"], "key": job["cache_key"],
                                       "material_hash": assessment["material_hash"],
                                       "priority": assessment["priority"]})
            # Remind only after the first recommendation has been confirmed sent.
            window = reminder_window(assessment["deadline"], self.now())
            if window and job.get("last_sent_material"):
                event_id = digest([job["id"], "deadline", assessment["deadline"], window])
                self.enqueue(event_id, {"kind": "reminder", "event_type": f"deadline{window}",
                                       "job_id": job["id"], "key": job["cache_key"],
                                       "material_hash": assessment["material_hash"], "priority": 4})

    def statistics(self):
        canonical = [j for j in self.jobs.values() if not j.get("duplicate_of")]
        pending = [j for j in canonical if j["state"] in {"pending", "evaluating"} and not j.get("closed")]
        return {"backlog_count": len(pending),
                "oldest_pending_hours": round(max([0] + [(self.now() - j["queued_at"]) / 3600 for j in pending]), 1),
                "needs_review": sum(j["state"] == "review_required" and not j.get("closed") for j in canonical),
                "source_records": len(self.jobs),
                "duplicate_records": len(self.jobs) - len(canonical),
                "filtered_records": sum(j.get("filter_reason") is not None for j in canonical),
                "model_calls_this_run": self.calls}

    def status_line(self):
        stats = self.statistics()
        return (f"状态：故障 {len(self.errors)}；提醒 {len(self.warnings)}；待评估 {stats['backlog_count']}；"
                f"需人工核验 {stats['needs_review']}；本轮模型调用 {self.calls}")

    def add_reports(self):
        events = self.store.scan("notifications")
        unknown = sum(e["state"] == "unknown" for e in events.values())
        stats = self.statistics()
        if self.errors or self.warnings or unknown or stats["backlog_count"] > 60 or stats["needs_review"]:
            self.enqueue(digest(["health", self.day()]), {"kind": "report", "priority": 5,
                         "text": "AI Job Radar 巡检\n" + self.status_line() +
                                 f"\n发送结果待核对：{unknown}。详情见云端运行记录。"})
        if datetime.fromtimestamp(self.now(), ZoneInfo("Australia/Melbourne")).weekday() == 4:
            counts, sample = {}, 0
            for job in self.jobs.values():
                if (job.get("duplicate_of") or job.get("assessed_at", 0) < self.now() - 7 * 86400
                        or job["state"] != "evaluated"):
                    continue
                sample += 1
                distinct = {(r["level"], r["label"].strip().casefold())
                            for r in job["assessment"]["requirements"] if r["gap"]}
                for key in distinct:
                    counts[key] = counts.get(key, 0) + 1
            if sample:
                lines = [f"{label}（{'必要' if level == 'required' else '加分'}）：{count} 个岗位"
                         for (level, label), count in sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:15]]
                self.enqueue(digest(["weekly", self.day()]), {"kind": "report", "priority": -1,
                             "text": clip(f"本周待核对要求 · 已评估样本 {sample} 个独立岗位\n" +
                                          "\n".join(lines) + "\n按标签计数；同义技能尚未合并。样本不代表整个市场。")})

    def deliver(self):
        events = self.store.scan("notifications")
        candidates = [(p, e) for p, e in events.items() if e["state"] == "pending"]
        candidates.sort(key=lambda pair: (-pair[1]["priority"], pair[1]["created"], pair[0]))
        reminder_batch = []
        for path, event in candidates:
            if not self.time_left(35):
                break
            if event["kind"] != "report":
                job = self.jobs.get("jobs/" + event["job_id"])
                if (not job or not self.eligible(job) or job["cache_key"] != event["key"]
                        or any(e.get("job_id") == job["id"] and e["state"] == "unknown" for e in events.values())):
                    event["state"] = "superseded"
                    self.save(path, event)
                    continue
                if event["kind"] == "reminder":
                    active = reminder_window(job["assessment"]["deadline"], self.now())
                    if event["event_type"] != f"deadline{active}":
                        event["state"] = "superseded"
                        self.save(path, event)
                        continue
                    reminder_batch.append((path, event))
                    continue
                text = render(job, job["assessment"], event["event_type"], event["id"], self.status_line())
            else:
                text = event["text"]
            self.send_batch([(path, event)], text)
            if event["state"] == "unknown":
                return  # Pause this dispatch pass after an ambiguous delivery.
        # Reminders have their own quota; aggregate up to eight in one message.
        for offset in range(0, min(len(reminder_batch), 24), 8):
            if not self.time_left(35):
                break
            batch = reminder_batch[offset:offset + 8]
            lines = ["临期提醒（独立于每日 5 条普通推荐）"]
            for _, event in batch:
                job = self.jobs["jobs/" + event["job_id"]]
                lines.append(f"{clip(job['title'], 80)} · {job['assessment']['deadline']['date']}\n{job['url']}\n{event['id'][:12]}")
            text = "\n\n".join(lines)
            if len(text.encode("utf-16-le")) > 7000:
                # Never truncate an aggregated event while marking it delivered.
                for one in batch:
                    if self.time_left(35):
                        job = self.jobs["jobs/" + one[1]["job_id"]]
                        self.send_batch([one], render(job, job["assessment"], one[1]["event_type"], one[1]["id"], self.status_line()))
            else:
                self.send_batch(batch, text)
            if any(event["state"] == "unknown" for _, event in batch):
                return

    def send_batch(self, batch, text):
        quota_path = "daily/" + self.day()
        paths = [path for path, _ in batch]
        job_paths = ["jobs/" + event["job_id"] for _, event in batch if event["kind"] == "ordinary"]
        ordinary = sum(event["kind"] == "ordinary" for _, event in batch)

        def claim(values):
            quota = values[quota_path] or {"model_calls": 0, "ordinary": 0}
            if quota["ordinary"] + ordinary > self.profile["ordinary_daily_limit"]:
                return False
            if any(values[p]["state"] != "pending" for p in paths):
                return False
            quota["ordinary"] += ordinary
            values[quota_path] = quota
            for path in paths:
                values[path].update(state="sending", owner=self.owner, attempted_at=self.now())
            return True
        if not self.atomic([*paths, quota_path], claim):
            return
        message_id, error = None, None
        try:
            message_id = self.telegram.send(self.chat_id, text)
            state = "sent"
        except DeliveryUnknown:
            state, error = "unknown", "delivery_uncertain"
        except TelegramError:
            state, error = "failed", "telegram_rejected"
        except Exception:
            state, error = "unknown", "delivery_exception"
        if state != "sent":
            self.errors.append(error)

        def finish(values):
            for path, event in batch:
                values[path].update(state=state, message_id=message_id, error=error, finished_at=self.now())
                if state == "sent" and event["kind"] == "ordinary":
                    values["jobs/" + event["job_id"]]["last_sent_material"] = event["material_hash"]
        # If this commit fails, leave sending in storage. Recovery marks it unknown.
        self.atomic([*paths, *job_paths], finish)
        for _, event in batch:
            event["state"] = state
            if state == "sent" and event["kind"] == "ordinary":
                self.jobs["jobs/" + event["job_id"]]["last_sent_material"] = event["material_hash"]

    def run(self, sources, fetch):
        if not self.acquire():
            return {"status": "skipped_overlap"}, 0
        started = self.now()
        try:
            self.recover()
            # Two source fetches at once, with periodic lease renewal. Every adapter
            # has bounded requests and a source deadline; exceptions never mean zero jobs.
            with ThreadPoolExecutor(max_workers=2) as pool:
                waiting = {pool.submit(fetch, s): s for s in sources if s.get("enabled")}
                while waiting:
                    done, _ = wait(waiting, timeout=10, return_when=FIRST_COMPLETED)
                    self.atomic([], lambda values: None)
                    for future in done:
                        source = waiting.pop(future)
                        try:
                            items, complete = future.result()
                            self.ingest(source, items, complete)
                        except LeaseLost:
                            raise
                        except Exception as error:
                            self.source_failed(source["id"], error)
            self.deduplicate()
            self.evaluate()
            self.prepare_notifications()
            self.add_reports()
            self.deliver()
            summary = {"status": "partial_failure" if self.errors else "success",
                       "started": started, "finished": self.now(), "run_id": self.owner,
                       "errors": self.errors, "warnings": self.warnings, **self.statistics()}
            self.save("runs/" + self.owner, summary)
            if not self.errors:
                self.save("control/heartbeat", {"last_success": self.now(), "run_id": self.owner})
            return summary, int(bool(self.errors))
        finally:
            self.release()
