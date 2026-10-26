# Public backup record

The original snapshot was prepared on 22 October 2026 (Australia/Melbourne) from cloud source commit `c8d3fdc3f6d3462232050dc1aeb9e5811119053b`, with runtime release `5cb3d5537af6`.

The 26 October 2026 incremental backup includes runtime repair revision `4d94daa78a35`. It adds persistent sanitized mail-card caching, resumable resolver allowance and a distinction between operational warnings and real failures. Only eight changed runtime/test files were copied into the existing sanitized public tree; configuration placeholders and infrastructure scripts remain unchanged. Documentation and content checksums were updated separately. This entry records the backed-up code and does not assert a successful deployment or live validation.

The downloaded Git archive was verified against SHA-256 `ab037ab8d811f5c7830eb14b8d7d8715fcd4373ae0a66b58eef513883918142c` before preparation. It contained 47 tracked files. The public copy uses fresh Git history; it does not publish the private cloud repository's history.

## Included and adapted

- All runtime modules, all eight setup/maintenance scripts, all twelve test files, the assessment schema, container recipe, build recipe and locked dependencies.
- Cloud project, project number, bucket references and Telegram bot username replaced by generic placeholders, including the acceptance-test fixtures.
- Public-source configuration retained; the two mail-source templates added from the deployed source schema with credentials omitted and activation disabled.
- Profile example aligned to the recorded 10/run, 30/day limits; candidate experience and work authorization remain unconfirmed examples.
- README and deployment guidance rewritten to remove personal/account state and separate current behavior from historical deployment records.
- Ignore rules expanded to exclude local credentials, private configuration, runtime data, raw mail and archive files.

## Excluded from publication

Production `SETUP_STATUS.json`, private profiles and deployment documents, tokens and passwords, personal email addresses, channel IDs, billing details, raw messages, fetched job bodies, model responses, Firestore state, logs and historical Git objects are not included. The original planning PDFs are not published.

This is a source backup with generic configuration templates, not a complete private-data or infrastructure export. Exact private restoration still requires the existing private cloud records and data. The cloud service is unchanged by this backup.

## Validation record

All 122 offline tests passed on 26 October 2026 after the public snapshot's incremental code and placeholder/template checks (Python 3.15.1). The test suite covers parsing, deduplication, evidence checks, quotas, provider backoff/cooldown, persistent mail-card reuse, resumable coverage, operational warning classification, outage escalation and Telegram delivery reconciliation. No cloud, mailbox, model or Telegram calls are needed for the unit tests.

The original 21 October deployment validation checked source ingestion and queued failures without a new model call because the daily allowance was exhausted. The 26 October record above covers code and offline validation only; consult the private deployment record for subsequent live acceptance results.

## Updating this backup

Future cloud edits are not synchronized automatically. Export the latest committed source into a separate review directory, apply the same redactions, add only public configuration templates, run tests and inspect the full staged diff before pushing. Never connect the private cloud working tree directly to the public remote or push its history wholesale. Do not add GitHub workflows or credentials unless deliberately changing this storage-only setup.

`BACKUP_MANIFEST.json` records SHA-256 hashes for all public content files other than itself. Update it when making a new reviewed snapshot.
