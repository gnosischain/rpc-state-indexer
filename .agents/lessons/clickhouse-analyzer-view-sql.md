---
name: clickhouse-analyzer-view-sql
symptom: "Code 184 ILLEGAL_AGGREGATION" or "Code 47/60 unknown identifier" creating 007 views
area: migrations
status: active
updated: 2026-07-21
---

## Symptom

Applying `007_views.sql` on a recent ClickHouse (new analyzer, e.g. 26.2) fails with
`Code: 184 ... argMax(col, tuple(..., col)) is found inside another aggregate function`, or
downstream views fail with `Code: 47/60 unknown identifier chain_id`. Fails under both
`enable_analyzer=1` and `0` (so it is not a toggle — the SQL is genuinely ambiguous).

## Root cause

Two new-analyzer behavior changes the original views relied on old semantics for:
1. **Alias == column collision.** `argMax(config_hash, tuple(insert_version, config_hash)) AS
   config_hash` (and `argMax(X, ...) AS X`, and aggregate args reusing an aliased name): the
   analyzer resolves the inner column name to the same-named SELECT alias -> "aggregate inside
   aggregate".
2. **`SELECT p.*` across joins keeps qualifiers in output names.** `v_publications_eligible`'s
   `SELECT p.*` over a 4-table join produced columns literally named `p.chain_id`,
   `p.config_hash`, ... so downstream views referencing `chain_id` could not find them.

## Fix / correct pattern

1. Give each aggregate's source a table alias and **qualify every column used inside an
   aggregate** (and its GROUP BY / SELECT refs) with it: `argMax(cr.config_hash,
   tuple(cr.insert_version, cr.config_hash)) AS config_hash ... FROM config_registry AS cr`.
   Applied to `v_config_registry_current`, `v_day_anchor_status`, `v_publication_status`.
   (`v_entity_registry_current` and `v_transfer_log_count_status` used the same pattern but
   were later dropped in the schema trim — see [[schema-trim]].)
2. Replace `SELECT p.*` with an **explicit column list** aliased to clean names
   (`p.chain_id AS chain_id, ...`) in `v_publications_eligible`.
3. **Also alias bare `alias.col` projections over a multi-source join.** `v_coverage_calendar`
   selected `c.chain_id, a.snapshot_date, ...` (no `AS`) over a 3-way join whose sources all
   carry `chain_id`; the analyzer kept the qualifier in the OUTPUT name (`c.chain_id`), so a
   downstream `WHERE chain_id = ...` failed with Code 47. This one was **missed** in the first
   pass and only surfaced at deploy time via the `status` command (views are lazy — 007 created
   it fine, it broke at query time). Fixed in migration `010`. A single leaked qualifier is
   detectable with `DESCRIBE <view>` (any column name containing a `.`).

Output column names stay identical, so the published-view contract and downstream views are
unchanged. Validate by executing every statement of the rendered migration against the real
server (every statement must return OK) before recording.

## How to avoid / detect

Grep new/edited views for `argMax(<x>, ...) AS <x>` and for `SELECT <alias>.*` over a join.
When editing migrations remember they are immutable once recorded ([[migrations-are-immutable]]);
`007` here was fixed because it had never successfully applied. Real error is masked by
[[cli-hides-real-db-error]].
