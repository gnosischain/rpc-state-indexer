---
name: migrations-are-immutable
symptom: migration checksum mismatch / checksum error on rerun
area: migrations
status: active
updated: 2026-07-21
---

## Symptom

`migrate` fails with a checksum error for a migration that previously applied fine, or a
schema change you made to an existing `NNN_*.sql` file will not take effect.

## Root cause

Migrations are recorded by full filename **and SHA-256** in the `migrations` table and are
idempotent on rerun. Editing an already-applied migration changes its checksum, which the
`MigrationRunner` (`src/rpc_state_indexer/storage/migrations.py`) treats as an error — by
design, so history cannot be silently rewritten.

## Fix / correct pattern

Never edit an applied migration. Add a **new** file `NNN_lowercase_name.sql` with the next
number (they start at `000`). Published views live in `007_views.sql`; evolve them by
adding a later migration, not by editing `007`.

## How to avoid / detect

Treat `migrations/` as append-only; see `migrations/AGENTS.md`. The integration test
(`tests/integration/clickhouse`) applies all migrations twice and asserts idempotence.
Related: [[clickhouse-published-contract]].
