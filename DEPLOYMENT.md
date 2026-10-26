# Deployment and restoration reference

This document describes the backed-up Google Cloud design. It does not initiate a deployment. GitHub is used only for source storage; no GitHub Actions, Pages deployment, cloud connection or runtime secrets are configured.

## Configuration map

| Setting | Public placeholder or reference |
| --- | --- |
| Google Cloud project | `your-gcp-project-id` |
| Project number | `123456789012` |
| Compute / Firestore region | `australia-southeast1` |
| Model location / model | `global` / `gemini-3.8-flash` as recorded in the snapshot |
| Telegram bot | `YourAIJobRadarBot`; real token and channel stay private |
| Build / backup buckets | `radar-build-123456789012` / `radar-backup-123456789012`; examples only |
| Runtime identity | Dedicated `radar-runner` service account |
| Build / scheduler identities | Separate least-privilege service accounts |

The global model endpoint is not an Australia-only processing guarantee. Confirm current model availability, pricing and credit eligibility before any new deployment.

Identifiers also appear in `radar/acceptance.py`, `radar/shutdown.py`, `scripts/configure_icloud_mail.py`, `scripts/check_icloud_mail.py`, `scripts/telegram_cloud_setup.py` and `scripts/prepare_cloud_build.py`. Replace their placeholders consistently in a private deployment checkout, and update the corresponding acceptance test fixture. The acceptance helper deliberately checks the selected project.

## Runtime resources

- Cloud Run Job `ai-job-radar`: one task, parallelism one, 1 vCPU, 512 MiB, 600-second timeout and zero automatic retries. The application stops within 480 seconds and has a shorter soft work deadline.
- Firestore Standard / Native in the selected region for jobs, source health, analyses, notifications, counters, leases, heartbeat and sanitized parsed-mail cache. Large bodies, assessments, notification text and parsed-card arrays should be exempted from indexing.
- Artifact Registry for immutable container images; Cloud Build uses a separate identity and the pinned base image/dependency lock.
- Secret Manager holds fixed versions of `radar-deployment`, `radar-profile`, `radar-sources`, `radar-telegram-token`, and optionally `radar-mail-icloud`.
- Cloud Scheduler `radar-daily`: `0 8,14,20 * * *`, timezone `Australia/Melbourne`, invoking the Cloud Run Jobs API with OAuth.
- Independent monitoring covers failed executions and missing successful runs (the recorded policy is approximately 24h50m). A successful Scheduler trigger is not a completed execution.

Runtime variables / mounts:

| Variable | Private resource |
| --- | --- |
| `RADAR_DEPLOYMENT_FILE` | Mounted deployment JSON |
| `RADAR_PROFILE_FILE` | Mounted candidate profile JSON |
| `RADAR_SOURCES_FILE` | Mounted approved source JSON |
| `TELEGRAM_BOT_TOKEN` | Fixed Secret Manager version exposed as an environment secret |
| `RADAR_ICLOUD_FILE` | Optional mail JSON mount containing `username` and `app_password` |

The deployed version used separate mount directories under `/secrets/`. The image includes the assessment schema but excludes runtime credentials and candidate data. Avoid downloaded service-account keys and broad Owner/Editor roles for runtime identities.

## Restoring from this code backup

1. Clone the repository into a private working directory. Keep the public repository free of production configuration and data.
2. Restore the private deployment/profile/source documents from the existing Secret Manager versions or a separate private backup. Review region, model, budgets, stop date, channel and approved source access.
3. For a new instance, adapt `config/deployment.example.json` and `config/profile.example.json`; keep them under ignored private filenames. Populate only verified candidate evidence. Mail templates are disabled until their credentials, folder and `not_before` activation Unix timestamp have been verified.
4. Provision or reuse the database, registry, service accounts, secrets and independent monitoring. A code clone does not restore Firestore jobs, quotas, delivery history or pending work. Recover those separately if continuity is required; starting with an empty database can lose deduplication history.
5. Run all offline tests. Build only after the deployment and spending review, using the pinned `Dockerfile` and `requirements.lock`. The `cloudbuild.yaml` file runs tests and builds an explicitly supplied `_IMAGE`.
6. Perform the bounded acceptance checks (`python -m radar.acceptance firestore`, `model`, `telegram`) after configuring `acceptance_authorized` and the required verification flags. Acceptance records reserve attempts before external calls. A synthetic fixture does not replace a real-source quality audit.
7. Deploy a reviewed immutable image digest and fixed secret versions. Verify source ingestion, evidence checks, Telegram acknowledgement and repeated-run deduplication. Set production activation flags only after the corresponding checks pass.
8. Enable scheduling after successful manual verification, and retain the previous image and private configuration versions for rollback.

`scripts/prepare_cloud_build.py` is a manual pilot helper. It expects reviewed private budget evidence under ignored `work/`, prepares dependency hashes, manages a private staging bucket and submits a build. It is not run by this repository and must not be used before its identifiers and assumptions are reviewed.

## Cost controls and stop dates

Application allowances in the public profile example are 10 AI calls/run, 30/local day and five ordinary recommendations/day. Counts include failed requests and retries. Reminders and health messages are managed separately. The limits are shared across all sources.

Configure project budget alerts, supported service controls and a planned stop before credits expire. Budget alerts and application quotas are not a guaranteed total billing ceiling. Storage, logs, secrets, registry images and delayed usage can remain billable after scans stop.

The original stop design pauses `radar-daily` and `radar-credit-stop` on 14 January 2027 UTC; the application refuses new work from 15 January 2027 UTC. These are snapshot dates, not a recommendation for a new account. `radar/shutdown.py` also contains the selected project, region and stop date. Review these together; never disable a shared billing account as part of application shutdown.

## Mail intake

The reader connects with TLS to iCloud and opens only the dedicated `AI Job Radar` folder in read-only mode (`EXAMINE` / `BODY.PEEK`). It implements no SMTP or destructive mailbox commands. Apple app-specific passwords themselves grant broader mailbox access; the application enforces the folder restriction.

Server rules can move and mark as read alerts from `jobalerts-noreply@linkedin.com` and `jobmail@s.seek.com.au`. The adapters validate supported senders/templates, remove recipient addresses and tracking parameters, and use bounded excerpts. Messages must be after activation and within seven days; analysis/delivery evidence must remain within 48 hours. A successful mailbox read with no new matching alert for 72 hours produces a coverage warning, rather than an execution failure.

`mail_cache` stores sanitized parsed cards keyed by parser version, source, raw-message hash and original receipt timestamp. It stores neither raw mail nor recipient/tracking data. Runtime uses the existing Firestore permissions and ADC-selected project; verify the selected project and persisted cache reads during acceptance. Cache hits preserve the original evidence timestamp and do not refresh expired cards. Resolver limits remain 80 HTTP requests and 45 seconds; remaining messages defer until a later scan. A bad message does not discard other valid cards, but partial coverage is recorded as a warning. All-unparsed messages, actual mailbox failures and cache storage failures still fail the source. Include this private cache in retention/backup reviews; it is not part of the public repository.

Generate mailbox credentials through the account owner's own authentication flow, enter them at the hidden prompt in `scripts/configure_icloud_mail.py`, and pin the stored secret version. `python3 -m scripts.check_icloud_mail` is a manual read audit; it does not call AI or send Telegram messages. Keep its output private because it may contain selected job information.

## Operations and failure recovery

Pause scanning before maintenance:

```bash
gcloud scheduler jobs pause radar-daily   --location=australia-southeast1 --project=your-gcp-project-id
```

A pause does not cancel an active execution. Resume with the corresponding `resume` command only after checking active jobs, allowance and stop date. A manual Scheduler `run` still uses the normal quotas.

Inspect completed Cloud Run executions, structured `radar_run` logs, Firestore run/source/job/notification records and `control/heartbeat`. An overlap exits as `skipped_overlap`. Warning-only runs retain `status="success"`, update the heartbeat and log `WARNING`; actual failures retain `partial_failure`, nonzero exit status and `ERROR`. The summary includes separate `warnings` and `errors` lists. Daily Telegram health reports include warning counts without generating a new report for each scan. Never reset counters simply to repeat paid calls.

ValueError evidence-check rejections remain withheld and retry at most under the existing evidence-validation policy; they are warnings. Malformed JSON, runtime errors and unexpected exceptions remain errors. Successful mailbox silence and deliberate cooldown are warnings; an actual source-fetch, storage or Telegram delivery error remains an operational failure.

Transient provider errors allow one short backoff retry; each attempt reserves quota independently. Continued errors stop AI requests for the run and persist a 15/30/60/120-minute cooldown. A non-retryable provider error uses a longer cooldown and fails immediately. A `Retry-After` above five seconds defers to a later run. Four provider failures on the same unchanged job require review. Validation failures have a separate two-attempt policy and retain private diagnostic evidence. Safe error codes exclude raw provider response bodies.

`control/model_service.last_success` records each actual validated AI assessment immediately, including when another request later fails in that run. A success clears the outage's `first_failure` anchor. Retryable failures are warnings while an actual successful assessment is less than 24 hours old; absent such a success, they escalate after 24 hours from the first recorded failure. If the last actual success is already 24 hours old, a new provider failure escalates immediately. For an older deployment migration, derive the latest actual success from stored `jobs.assessed_at`, rather than treating a completed scan as an AI success.

The 26 October 2026 repair code is source revision `4d94daa78a35` and has 126 passing offline tests. This public code-backup record does not assert that the repair has been deployed or live-verified.

For an unknown Telegram outcome, inspect the private channel and event identifier first. `scripts/reconcile_delivery.py` obtains the run lease and records a verified resolution; it never sends a message itself. Do not automatically resend an uncertain delivery.

Rollback by redeploying the previously accepted immutable image and compatible pinned secret versions from private deployment records. Keep Firestore state and daily quotas intact. Public-source retries do not bypass access challenges, TLS validation or redirect restrictions. Keep failure monitoring active while diagnosing a fault.

## Failure-email configuration

The 26 October deployment replaces the repeating log-match failure policy with a recovery-aware PromQL condition. It compares the latest positive `radar_actionable_failure` event timestamp against the latest successful recovery for the same job, using a 25-hour history and one-minute subqueries. Main-job recovery uses `radar_success`, which excludes overlap skips; acceptance and stop jobs use their successful Cloud Run execution metric. A missing success cannot clear a failure, and a different job's success cannot clear this job. A later recovery clears the condition. The alert strategy uses `notificationPrompts=["OPENED"]`, has no renotify strategy, and sets ERROR severity explicitly. Keep the former hourly log policy disabled and retain independent missing-success monitoring.

Known nonretryable or sustained AI failures remain operational failures during cooldown and quota-exhausted or empty-queue scans. They clear only after a validated model assessment, so a scan that merely defers work cannot falsely acknowledge recovery.

## Private backup responsibilities

Keep the following independently protected: Secret Manager versions, private profile, deployment record, channel details, Firestore state, cloud configuration and retained rollback images. A private Git bundle contains source history only; it does not include ignored private configuration or the runtime database. This public repository intentionally starts with a fresh sanitized history.
