---
name: compute-blocked-by-orphan-started-attempts
symptom: "compute fails `source census not complete ({\"daily_cl_liquidity\": {\"published\": 2519, \"started\": N}})` on a day whose census is fully published; pool_liquidity_profile missing that day; a re-run fails in seconds"
area: census
status: mitigated
updated: 2026-10-21
---

## Symptom

The compute CronJob failed on 2026-10-18, 19 and 20, for snapshot dates 2026-10-17, 18 and 19. Each attempt waited the full `COMPUTE_WAIT_SECONDS` (90 min), then raised `compute cl_profile for <D>: source census not complete ({"daily_cl_liquidity": {"published": 2519, "started": 3|2|7}})`.

`census_publications` held all 2519 targets for each of those days, published before 03:10 UTC. `pool_liquidity_profile` had no rows for them. On 2026-10-21 a one-shot `compute --date D` failed in about 20 s with the same message, because dates older than yesterday are checked only once.

## Root cause

`_census_source_counts` (`src/rpc_state_indexer/service.py`) counted every `census_attempts FINAL` row with `status = 'started'` for the date.

An attempt writes its `started` row first, and its `failed` row from the `except` block (`core/census.py`, `run_token`/`run_pool`). The attempt stays `started` forever when either:
- that `failed` insert itself fails, e.g. a code 241 when the warehouse is at its memory cap;
- the pod dies mid-attempt.

The daemon's next cycle censuses the target again under a new attempt id and publishes it. The day is then complete, but the orphan still counts as in flight. Nothing ever clears it, so waiting does not help.

How common it is: in the 30 days to 2026-10-21, chain 100 had 81 such attempts across 4 jobs, and every one was on a target that later published.

## Fix / correct pattern

The started count now excludes targets that already have a publication for the same chain, job and date: `(target_kind, target_address) NOT IN (... census_publications ...)`.

Compute reads only `v_*_published`, so an unpublished attempt on a published target cannot change its input. An attempt on a target with no publication still blocks.

Test: `tests/unit/test_compute_guard.py::test_started_count_skips_targets_already_published_that_date`. After the image is deployed, recompute each missed day with a one-shot `compute --date D` (private runbook 22, "Compute day missing or partial").

## How to avoid / detect

When compute reports `source census not complete`, compare the `started` count with `census_publications` for that date before waiting or re-running. If every target is published, the blockers are orphans.

Not covered: an orphan on a target that never published that day still blocks. There were none in the 30 days to 2026-10-21; fixing it would need a staleness rule on `heartbeat_at`.

Never clear an orphan by deleting or rewriting `census_attempts`, because it is the evidence (AGENTS.md). Related: [[backfill-crashes-on-transient-clickhouse-blip]], [[backfill-retry-burns-backoff-on-discovery]].
