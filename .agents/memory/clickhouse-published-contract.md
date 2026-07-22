---
name: clickhouse-published-contract
type: invariant
updated: 2026-07-21
---

Storage is ClickHouse only (database from `CLICKHOUSE_DATABASE`, currently
`rpc_indexer_gnosis`). The **only** consumer-facing contract is the publication-gated
views defined in `migrations/007_views.sql`:

- `v_token_balances_published`, `v_token_scalars_published`, `v_pool_token_balances_published`
- plus `v_publications_current`, `v_coverage_calendar`, and conflict/health views.

Never build a consumer on raw tables (`token_balances`, `census_attempts`, …): raw rows
include unpublished/failed diagnostic attempts. Published views join through the
append-only publication gate and the current effective-config hash — see
[[config-change-triggers-reindex]].

Schema is applied by numbered migrations `000`–`007` (checksum-verified, append-only) —
see [[migrations-are-immutable]] and `migrations/AGENTS.md`.
