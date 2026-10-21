# Public backup record

Snapshot prepared on 22 October 2026 (Australia/Melbourne) from cloud source commit `c8d3fdc3f6d3462232050dc1aeb9e5811119053b`. The included runtime corresponds to the provider-recovery source release `5cb3d5537af6`.

The downloaded Git archive was verified against SHA-256 `ab037ab8d811f5c7830eb14b8d7d8715fcd4373ae0a66b58eef513883918142c` before preparation. It contained 47 tracked files. The public copy uses fresh Git history; it does not publish the private cloud repository's history.

## Included and adapted

- All runtime modules, all eight setup/maintenance scripts, all ten test files, the assessment schema, container recipe, build recipe and locked dependencies.
- Cloud project, project number, bucket references and Telegram bot username replaced by generic placeholders, including the acceptance-test fixtures.
- Public-source configuration retained; the two mail-source templates added from the deployed source schema with credentials omitted and activation disabled.
- Profile example aligned to the recorded 10/run, 30/day limits; candidate experience and work authorization remain unconfirmed examples.
- README and deployment guidance rewritten to remove personal/account state and separate current behavior from historical deployment records.
- Ignore rules expanded to exclude local credentials, private configuration, runtime data, raw mail and archive files.

## Excluded from publication

Production `SETUP_STATUS.json`, private profiles and deployment documents, tokens and passwords, personal email addresses, channel IDs, billing details, raw messages, fetched job bodies, model responses, Firestore state, logs and historical Git objects are not included. The original planning PDFs are not published.

This is a source backup with generic configuration templates, not a complete private-data or infrastructure export. Exact private restoration still requires the existing private cloud records and data. The cloud service is unchanged by this backup.

## Validation record

All 102 offline tests passed on 22 October 2026 after the public snapshot's placeholder/template edits (Python 3.15). The test suite covers parsing, deduplication, evidence checks, quotas, provider backoff/cooldown and Telegram delivery reconciliation. No cloud, mailbox, model or Telegram calls are needed for the unit tests.

On 21 October the recorded deployment validation checked source ingestion and queued failures without a new model call because the daily allowance was exhausted. This snapshot does not assert a later live model recovery or current source availability.

## Updating this backup

Future cloud edits are not synchronized automatically. Export the latest committed source into a separate review directory, apply the same redactions, add only public configuration templates, run tests and inspect the full staged diff before pushing. Never connect the private cloud working tree directly to the public remote or push its history wholesale. Do not add GitHub workflows or credentials unless deliberately changing this storage-only setup.

`BACKUP_MANIFEST.json` records SHA-256 hashes for all public content files other than itself. Update it when making a new reviewed snapshot.
