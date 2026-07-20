# Changelog

All notable changes to Sieve are documented here.

## Unreleased

- No changes yet.

## 1.1.0-beta.1 - 2026-07-20

### Added

- Private multi-user tenancy with immutable UUIDv4 identities, administrator invitations, membership controls, and UUID-scoped preferences, revisions, decisions, retries, deduplication, and delivery.
- Single-use, 24-hour invitations stored only as cryptographic hashes. Telegram numeric user and private-chat IDs authenticate delivery; usernames, display names, contacts, and phone numbers are not persisted.
- Per-user cross-group near-duplicate suppression with canonical URL and strong product/model/price matching over a 24-hour window.
- A persistent Telegram delivery outbox with restart recovery, bounded exponential retry, `retry_after` support, and visible permanent failures.
- Multi-user fan-out over one shared raw BM25 corpus, with per-user terms and alias expansion.
- Health visibility for active users, BM25 readiness, queue depth, outbox depth and age, retries, and failed deliveries.
- Privacy-safe structured failure logs with provider method/status/code, retry timing, consecutive counts, alert decisions, and bounded polling backoff; unexpected polling, command, and outbox errors are contained and retain tracebacks.
- `/account`, `/invite`, `/users`, `/disable`, and `/enable` membership commands.
- QR-only Telethon session authorization; Sieve no longer accepts a phone number argument.

### Changed

- Existing single-owner databases migrate transactionally to one generated administrator UUID while retaining preference history, decisions, retries, deliveries, and corpus data.
- Configuration is consolidated in `config/config.yaml`; `preferences.admin_telegram_user_id_env` replaces owner settings and `preferences.max_users` defaults to 10.
- Package version is `1.1.0b1` and the release tag is `v1.1.0-beta.1`.

### Removed

- Deployment-wide and per-source promotion shadow modes, silent promotion delivery, the sink `shadow` argument, and the “Envio de teste” banner.
- Legacy owner/chat configuration keys, sink chat destination configuration, layered `extends` configuration, and phone-number CLI arguments.

### Upgrade warning

- Back up the SQLite state before upgrading. Downgrades require restoring the pre-migration backup; an upgraded database is not downgraded in place.
- Review removed configuration keys before starting this version. BM25's independent `off`/`shadow`/`live` auto-forward calibration mode remains available.
