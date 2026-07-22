---
name: supply-residual-metric-scoped-jobs
symptom: "rpc_indexer_supply_residual_ppm reads ~1e6 (100%) for a token that actually reconciles; supply-residual alert fires falsely"
area: observability
status: active
updated: 2026-07-22
---

## Symptom

The `rpc_indexer_supply_residual_ppm` gauge (and its dashboard panel / alert) shows ~1,000,000 ppm
(100%) for a token whose holder-sum vs `totalSupply` actually reconciles exactly. The value looks
nondeterministic run-to-run. A reconciliation check against `v_publications_eligible` shows the
`full_supply` job has `residual_ppm = 0`, but the same `(token, date)` also has rows for
`scoped` jobs (`daily_token_supply`, `daily_treasury`) with `observed_sum_raw = 0` and
`residual_ppm = 1e6`.

## Root cause

`CensusRunner._publish_token` computed and emitted `SUPPLY_RESIDUAL_PPM` for **every** publication
whose `reference_supply` (the `totalSupply` scalar) was present — regardless of integrity mode.
But only `full_supply` / `scaled_full_supply` jobs sweep the **full holder universe**; `scoped`
jobs (`universe: supply_probe` / `treasury`) read `totalSupply` as a scalar yet only sum a subset
of holders, so their `observed_sum` is ~0 and the residual is a meaningless ~100%.

Two compounding faults: (1) no integrity-mode guard on the emit; (2) the gauge is labelled **only
by `token`**, not by job — so the three WXDAI publications (`curated_balances`/`token_supply`/
`treasury`) all wrote the *same* gauge series and overwrote each other. Whichever job published
last won; if a scoped job landed last, the token's residual read 100% even though it reconciled.
The raw data was correct throughout — this was purely a metric-quality/false-alarm bug.

## Fix

Gate the emit to the reconciling modes (see [`core/census.py`](../../src/rpc_state_indexer/core/census.py),
`_publish_token`):

```python
if reference_supply is not None and job.integrity_mode in (
    IntegrityMode.FULL_SUPPLY, IntegrityMode.SCALED_FULL_SUPPLY,
):
    ... SUPPLY_RESIDUAL_PPM.labels(token=token.symbol).set(residual_ppm)
```

A token is either standard-erc20 (`full_supply`) or an aToken (`scaled_full_supply`), never both,
so exactly one reconciling job publishes per token — the token-only label no longer collides.
Regression tests: `test_supply_residual_gauge_skips_scoped_jobs` (scoped → gauge unset) and
`test_supply_residual_gauge_set_for_full_supply_jobs` in `tests/unit/test_census.py`.

## How to avoid / detect

A per-token gauge is safe **only** when at most one publication path writes it per token; if
multiple jobs touch a token, either add a distinguishing label (`job`) or gate the emit so only one
job qualifies. When reconciling holder-sum against supply, always restrict to `full_supply` /
`scaled_full_supply` — `v_publications_eligible.reference_supply_raw` is populated for scoped jobs
too, so filter on the job/mode, not merely on "supply is non-null". The stored
`observed_sum_raw = 0` on a scoped publication is truthful (not a coerced failure), so this is a
reporting fix, not a data-integrity one. Related: [[never-coerce-failure-to-zero]].
