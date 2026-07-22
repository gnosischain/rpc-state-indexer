---
name: schema-trim
type: project
updated: 2026-07-22
---

Phase 0 schema trim (2026-07, pre-launch fresh-start reset — all indexed data was droppable).
**Removed 6 tables (4 dead + 2 load-bearing folded/rewired)**, keeping the core guarantees
(failure-vs-zero, publication gate, gap-free discovery, per-target reindex safety):

- `repair_requests` — no repair command exists (aspirational).
- `rpc_benchmarks` — `bench` now prints its p50/p95/batch-size in the `benchmark_complete` event.
- `endpoint_capabilities` — the startup/standalone probe now **logs** an `endpoint_probe` event
  instead of persisting; `probe --persist` is a deprecated no-op. The probe itself
  (`rpc/capabilities.py::probe_endpoint_capabilities`, which sets endpoint health flags) is unchanged.
- `transfer_log_counts` (+ `v_transfer_log_count_status`/`_conflicts`, + the `validate`
  `transfer_log_conflicts` check) — an extra discovery cross-check; gap-free discovery via
  `discovery_ranges`/`holder_universe` is unaffected. **Bonus:** removed `discovery_service._block_timestamps`,
  eliminating one `eth_getBlockByNumber` per active block during discovery.

**Load-bearing pair (also done in the same fresh-start reset):**
- `census_batches` → **folded into `census_attempts.batches_json`** (a canonical-JSON array of the
  per-batch verification evidence: `batch_sequence`, `executor_kind`, `block_reference_kind`,
  `anchor_hash`, `body_call_count`, `provider_groups`, `result_digest`, `verified`). Written on the
  verified attempt row in `_publish_pool`/`_publish_token` via `CensusRunner._batches_json`; the old
  per-batch persist path (`insert_batches`/`_batch_rows`) and `v_census_batches_current` are gone.
  Unpack in SQL with `ARRAY JOIN JSONExtractArrayRaw(batches_json)` (see `docs/runbook.md`).
- `entity_registry` (+ `v_entity_registry_current`, + `v_discovery_frontier`) — dropped. Its only
  runtime role, the entity coverage window, is already carried by `config_registry`
  (`coverage_start`/`coverage_end`); discovery reads deploy blocks from `TokenConfig.deployment_block`
  in config, not the DB. Frontier coverage is now inspected directly from `discovery_ranges` (runbook).

Execution: for a fresh start the source migrations were edited to the clean schema (tables removed from
`001`/`002`/`003`/`006`, views from `007`; `batches_json` added to `census_attempts` in `003`; the
temporary `008` drop migration deleted), then **all objects were dropped and re-migrated** from
`000`-`007`. Migrations are immutable again after this one-time reset.

Related: [[gnosis-catalog-scale]], [[migrations-are-immutable]], [[clickhouse-analyzer-view-sql]].
