# AI Job Radar

Public source-code backup of a Google Cloud job radar for Australian academic and Melbourne industry opportunities.

**This repository stores code and documentation only.** The running radar stays on Google Cloud. GitHub Actions is disabled; there are no deployment workflows, schedules, repository secrets or cloud credentials here. Uploading or cloning this repository does not run the radar.

中文说明：这是 AI Job Radar 的公开代码备份。实际扫描、去重、AI 分析和 Telegram 推送仍在 Google Cloud 运行。个人资料、邮箱密码、Bot token、频道编号、原始邮件和运行数据库不公开。

## What the radar does

1. Read selected public career sites and optional official job-alert emails.
2. Normalize job records and deduplicate corroborated URLs or requisition identities before analysis.
3. Apply geography/topic filters and assess remaining jobs with Vertex AI Gemini.
4. Validate structured output and supporting source quotations.
5. Send bounded recommendations and reminders to a private Telegram channel.
6. Persist queue, quota, delivery and health state in Firestore.

It does not submit job applications. Matching scores are prioritization aids, not hiring probabilities. Missing candidate evidence stays unknown.

## Included sources

| Source | Adapter / snapshot state |
| --- | --- |
| Culture Amp | Greenhouse; public adapter enabled |
| MathJobs Australia | RSS discovery and job details; enabled |
| AustMS Careers | Public listings and reviewed employer links; enabled |
| University of Melbourne | Workday; enabled |
| RMIT University | Workday; enabled |
| Monash University | PageUp; enabled |
| Times Higher Education UniJobs Australia | Public discovery and relevant full descriptions; enabled |
| SEEK | Official email alerts via iCloud; optional template disabled until configured |
| LinkedIn | Official email alerts via iCloud; optional template disabled until configured |
| La Trobe / Deakin | Disabled following access challenges |

Coverage is selective. Swinburne is not connected. SEEK and LinkedIn are email adapters, not authenticated website scrapers. Public adapters retain their reviewed snapshot settings; production activation still defaults to false.

## Reliability and limits

The recorded deployment scans at 08:00, 14:00 and 20:00 in `Australia/Melbourne`, with 10 AI attempts per run, 30 per local day and five ordinary recommendations per day. Every AI retry consumes an allowance. Unprocessed jobs remain queued.

Transient model failures permit one short backoff retry per job per run. Continued failure stops further AI calls in that run and persists a cooldown. Four provider failures for an unchanged job require review. Invalid evidence has a separate two-attempt limit. Unknown Telegram delivery requires reconciliation before any resend.

Fenced leases, atomic quota reservations, cross-source identity checks and a persistent outbox protect against overlapping runs and repeated recommendations. Source failures remain visible. Firestore scans have a 2,500-record safety bound.

The 26 October 2026 repair adds a private Firestore cache of sanitized parsed mail cards. Repeated polls reuse cards with their original receipt timestamps, so old messages no longer consume the redirect allowance repeatedly. Remaining messages resume on a later scan when the 80-request/45-second resolver allowance is reached; valid messages are retained and coverage warnings remain visible. Raw mail, recipient addresses and tracking URLs are excluded from the cache.

Safely withheld evidence-check rejections, a recent transient model cooldown, 72 hours without a new matching email, and isolated retryable AI failures are warnings. Warning-only runs retain a successful heartbeat and log `WARNING`; their health report is deduplicated daily in Telegram. A retryable provider outage escalates to a failure after 24 hours without a successful validated assessment (or from its first recorded failure if no success exists). Nonretryable provider errors, actual source/storage/delivery failures, malformed model responses and unexpected runtime errors remain failures.

The cloud failure monitor compares the latest fatal event with the latest successful recovery for each job over a 25-hour history. Later recovery clears an older failure. Only incident-opening email is enabled; repeated and closure emails are disabled. The former repeating log-match policy is retained disabled. Independent missing-success monitoring remains active.

## Repository contents

| Path | Purpose |
| --- | --- |
| `radar/` | Runtime, source adapters, AI validation, storage, delivery and shutdown |
| `scripts/` | Manual setup, validation, build and recovery helpers |
| `tests/` | Offline unit tests with synthetic or mocked inputs |
| `config/` | Assessment schema, generic profile/deployment examples and source settings |
| `Dockerfile`, `requirements.lock` | Pinned container base and dependency hashes |
| `cloudbuild.yaml` | Manually invoked Google Cloud build recipe; no GitHub trigger |
| `DEPLOYMENT.md` | Configuration map and restoration procedure |
| `BACKUP.md`, `BACKUP_MANIFEST.json` | Snapshot provenance, exclusions and content checksums |

## Offline verification

Use Python 3.12 or later:

```bash
python3 -m unittest discover -s tests -v
```

The unit suite uses mocks and an in-memory store; it does not need cloud, mailbox or Telegram credentials. The current snapshot has 126 tests. Runtime dependencies in `requirements.lock` were locked for the pinned Linux container; those wheel hashes are not a portable macOS dependency lock.

## Restoring the service

Read [DEPLOYMENT.md](DEPLOYMENT.md) before provisioning or deploying. Deployment identifiers in helper scripts are deliberately replaced by `your-gcp-project-id`, `123456789012` and `YourAIJobRadarBot`. These must be reviewed and configured together. Example authorization flags are false, the Telegram destination is unset, and the candidate profile contains no verified personal experience.

This public snapshot does not contain the private configuration or Firestore state required for exact service restoration. Those remain in the existing private cloud environment and private backups. The backed-up code includes the 21 October provider-throttling repair and the 26 October 2026 mail-cache and operational-warning repair. The latest backed-up runtime source revision is `88a906f06af2`, which includes the additional fatal-outage recovery guard. Inclusion in this backup does not establish its final live verification.

The recovery guard preserves known fatal AI outages during cooldown or a no-call scan, until an actual validated assessment clears the stored state. An earlier live run of base repair `4d94daa78a35` completed on 26 October 2026 at 11:47:26 Australia/Melbourne with 10 AI attempts and zero operational errors. That run predates the final guard release; final guard verification completed at 13:19:40 Australia/Melbourne with zero operational errors and zero new AI calls; it verified deployment, source intake, quota and health handling.
