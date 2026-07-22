---
name: indexer-two-layer-architecture
type: project
updated: 2026-07-22
---

The indexer separates **verified contract ingestion** from **derived computation**. New
contract → add an ingestion collector; new metric → add a compute module. Neither layer
touches the other.

- **Layer 1 — ingestion (verified, shipped).** `collectors/` modules land raw on-chain
  primitives pinned to an anchor, sentinel-verified + read-back-digested, published behind
  `v_*_published`. **No math here.** Members: `erc20`, `atoken`, `pools`, `balancer`, and
  (2026-07) `cl_liquidity` (Uniswap V3 + Algebra tick primitives → `pool_cl_state` +
  `pool_tick_liquidity`, migration 008, job `daily_cl_liquidity`, `IntegrityMode.CL_LIQUIDITY`).
  CL ingestion is live-verified on Gnosis: both protocols publish and the ΣliquidityNet
  invariants hold (see [[cl-liquidity-profile]], [[cl-bitmap-convention-differs]]).

- **Layer 2 — compute (derived, shipped 2026-07).** `compute/` package: modules implement the
  `ComputeModule` protocol (`base.py`), listed in `REGISTRY` (`__init__.py`). Each reads only
  `v_*_published` (never RPC) and writes a **derived** table carrying provenance
  (`source_attempt_id` + `source_result_digest`) back to the source publication. Run via
  `compute --date <d> [--module <name>]` → `service.run_compute` (repository only — **no**
  runtime/anchor/writer-guard). Idempotent: ReplacingMergeTree on the derived grain; re-running
  reproduces identical data rows (timestamps are DB `DEFAULT`s, not in the Python payload).
  First module `cl_profile` (`pool_liquidity_profile`, migration 009): walks sorted ticks
  accumulating `liquidity_net` into `[tick_lower, tick_upper)` active-L segments; live-verified
  that the current-tick segment's active_L == `liquidity()` for both protocols. Derived tables
  are NOT behind the RPC publication gate — verification is inherited via provenance + recompute.

Dispatch pattern: `CensusRunner.run_pool` branches by `job.integrity_mode` — `CL_LIQUIDITY`
routes to `ClLiquidityCollector` + `_persist_pool_cl_result`/`_publish_pool_cl`; other pool
jobs use the reserve/balancer path. Related: [[gnosis-catalog-scale]], [[schema-trim]].
