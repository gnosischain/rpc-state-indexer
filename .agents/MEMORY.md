# Memory index

Durable, non-obvious facts about `rpc-state-indexer`. One fact per file in
[`memory/`](memory/). Read before non-trivial work; add a line here when you add a fact.
Convention: [`README.md`](README.md).

- [multicall3-boundary](memory/multicall3-boundary.md) — block 21,022,491 splits the legacy and Multicall3 execution regimes.
- [clickhouse-published-contract](memory/clickhouse-published-contract.md) — consumers read only `v_*_published` views, never raw tables.
- [settings-env-loading](memory/settings-env-loading.md) — the app never auto-reads `.env`; Compose/shell must inject it.
- [single-writer-heartbeat](memory/single-writer-heartbeat.md) — one writer per chain, enforced by a heartbeat + stale window.
- [provider-group-quorum](memory/provider-group-quorum.md) — legacy verification needs 2 genuinely independent provider groups.
- [deploy-run-sequence](memory/deploy-run-sequence.md) — verified deployment order and the current `.env` status.
- [balancer-vault-custody](memory/balancer-vault-custody.md) — Balancer reserves come from the Vault (V2/V3), not balanceOf(pool).
- [gnosis-dex-factories](memory/gnosis-dex-factories.md) — confirmed factory/vault addresses + pool-creation event topics for enumeration.
- [gnosis-catalog-scale](memory/gnosis-catalog-scale.md) — full catalog (3.4k tokens / 4k pools), the jobs, and the scale caveat.
- [schema-trim](memory/schema-trim.md) — Phase 0 dropped 6 tables (fresh-start reset); census_batches folded into census_attempts.batches_json, entity_registry gone.
- [cl-liquidity-profile](memory/cl-liquidity-profile.md) — CL tick primitives + live-confirmed Uniswap V3 / Algebra struct layouts, signed int128 liquidityNet, Σ invariants.
- [indexer-two-layer-architecture](memory/indexer-two-layer-architecture.md) — verified ingestion collectors vs derived compute modules; CL ingestion + compute layer shipped.
- [catalog-incremental-refresh](memory/catalog-incremental-refresh.md) — watermarked `enumerate --incremental` + additive `assemble` + `make refresh-catalog`; per-target hash keeps it reindex-safe.
- [deployment-and-observability](memory/deployment-and-observability.md) — GHCR CI, Terraform/EKS deploy in the infra repo, new metrics, Grafana dashboard + alerts; CLICKHOUSE_USER + daemon-scoping gotchas.
- [treasury-sweep-pipeline](memory/treasury-sweep-pipeline.md) — discovery-driven treasury tracking: wallet sweep → discovered token selector → scoped census; only the wallet CSV is curated.
