# Lessons index

Traps this codebase has hit, indexed by symptom. One lesson per file in
[`lessons/`](lessons/). Search this by the symptom you are seeing before debugging; add a
line here when you record a lesson. Convention: [`README.md`](README.md).

- ["suspect zero" / a balance reads 0](lessons/never-coerce-failure-to-zero.md) — never coerce a failed/missing read to zero; fail closed.
- [discovery gap / a log range was skipped](lessons/discovery-must-fail-closed.md) — discovery must raise and stop, never swallow a failing range.
- [migration checksum mismatch on rerun](lessons/migrations-are-immutable.md) — never edit an applied migration; add a new numbered file.
- [published rows vanished after a catalog edit](lessons/config-change-triggers-reindex.md) — a config-hash change drops old publications; reindex.
- [startup refuses: another writer is active](lessons/writer-heartbeat-refusal.md) — single-writer guard; wait out the stale window after a crash.
- [opaque "migration failed (DatabaseError)"](lessons/cli-hides-real-db-error.md) — the CLI hides the real error; reproduce via a direct client to see the code.
- [ClickHouse Code 516 auth fails though password looks right](lessons/env-password-quoting.md) — quotes in .env are sent as literal password chars; keep secrets unquoted.
- [ClickHouse Code 497 ACCESS_DENIED on migrate](lessons/clickhouse-cloud-migration-grants.md) — service user needs CREATE DATABASE/VIEW + DROP VIEW grants.
- [Code 184/47/60 creating 007 views on new analyzer](lessons/clickhouse-analyzer-view-sql.md) — qualify aggregate columns; replace SELECT p.* with explicit columns.
- ["docker compose build never does anything" / stale image](lessons/docker-compose-build-noop.md) — profile-gated services need an explicit name or `--build`.
- [census "eth_getBlockByNumber exhausted RPC endpoints"](lessons/anchor-genesis-archive-gating.md) — header reads were wrongly gated by the state-archive floor; genesis read failed.
- [Gnosis public archive can't eth_call at pre-EIP-2930 blocks](lessons/gnosis-public-archive-eip2930.md) — use a real archive node for deep history.
- [DEX pool reserves publish as ~0](lessons/balanceof-pool-zero-for-vault-dexes.md) — Vault-custody DEXs (Balancer) need a Vault collector, not balanceOf(pool).
- [Algebra/Swapr V3 tick discovery finds no ticks](lessons/cl-bitmap-convention-differs.md) — Uniswap tickBitmap keys on compressed tick, Algebra tickTable on raw tick.
- [backfill aborts at early dates: "TOK: ValueError" per token](lessons/backfill-aborts-on-not-deployed-targets.md) — `_active` must skip targets whose deployment_block > anchor; enumerated targets have no date_start.
- [supply_residual_ppm reads 100% for a token that reconciles](lessons/supply-residual-metric-scoped-jobs.md) — only emit the residual for full_supply/scaled_full_supply jobs; scoped jobs read totalSupply but don't full-sweep, and the token-only gauge label collides.
- [backfill fails every date after a point; census_errors empty; hot tokens stuck](lessons/discovery-wedges-on-unrecognized-getlogs-limit.md) — an unrecognized provider getLogs limit ("query exceeds max results") wasn't classified as PROVIDER_LIMIT, so discovery never subdivided; check discovery_ranges, add the marker.
- ["backfill failed (DatabaseError)"; ingest stops mid-range then grinds the compute loop](lessons/backfill-crashes-on-transient-clickhouse-blip.md) — a transient ClickHouse Cloud node restart crashed the whole ingest because run_backfill only caught JobRunError; widened to catch any per-date exception.
