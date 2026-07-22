# AGENTS.md — migrations/

ClickHouse schema, applied by `MigrationRunner`
(`../src/rpc_state_indexer/storage/migrations.py`) via `rpc-state-indexer migrate`. Root
rules: [`../AGENTS.md`](../AGENTS.md).

## Rules (append-only, immutable)

- Files match `NNN_lowercase_name.sql`, numbered from `000` with no gaps. Current set is
  `000`–`007`.
- Applied migrations are recorded by filename **and SHA-256** and are idempotent on rerun.
- **Never edit an applied migration.** Changing its bytes changes its checksum and `migrate`
  errors by design. To change schema, add the next-numbered file.
  See [`migrations-are-immutable`](../.agents/lessons/migrations-are-immutable.md).
- The published-view contract lives in `007_views.sql` (`v_*_published` and conflict/health
  views). Evolve views with a later migration, never by editing `007`. Background:
  [`clickhouse-published-contract`](../.agents/memory/clickhouse-published-contract.md).

## Adding a migration

1. Create `migrations/008_<name>.sql` (next number).
2. Keep it forward-only and idempotent where possible (`IF NOT EXISTS`, additive columns).
3. The integration test (`tests/integration/clickhouse`) applies all migrations twice and
   asserts idempotence — run it when ClickHouse is available.
4. The container entrypoint does not wait for ClickHouse; migrations are run explicitly
   (compose `migrations` profile) before daemon/jobs.
