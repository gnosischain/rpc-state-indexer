---
name: backfill-retry-burns-backoff-on-discovery
symptom: "backfill retry pod publishes (almost) nothing yet exits 1: backfill_date_failed on a date whose targets are all census_skipped_published; discovery_failed code 241 on discovery_ranges"
area: service
status: active
updated: 2026-09-25
---

## Symptom

A backfill Job's first pod publishes almost the whole range and fails a few targets (e.g. a
ClickHouse code 241 on one token). The retry pod has almost nothing left to do, but it
logs `discovery_complete` for every `full_holders` token on every date, then
`discovery_failed` (a code 241 reading `rpc_state_indexer.discovery_ranges`) and
`backfill_date_failed ["USDC: DatabaseError"]` on a date where every target logged
`census_skipped_published`. It exits 1, and the Job starts another identical pass. Seen
2026-09-25 on `--job daily_curated_balances --from 2026-09-08 --to 2026-09-24`: the first pod
published 1,077 target-days and failed WXDAI 2026-09-20. The second pod published only WXDAI,
then died on USDC's discovery on 2026-09-23. A third pod was needed to exit 0.

## Root cause

`census()` ran `discover()` for every active token of every selected `full_holders` job
BEFORE the per-job skip gate (`published_target_addresses`). So every date re-read discovery
state even when nothing was pending, and a discovery error on a fully published date failed
that date. On a warehouse at its memory cap, each retry repeated the load that made it fail,
for no benefit, up to the Job's `backoff_limit`.

## Fix / correct pattern

`census()` (`src/rpc_state_indexer/service.py`) now runs the skip gate for every selected job
first. Discovery (`_advance_discovery`) then covers only the pending targets of jobs where
`_needs_holder_discovery` holds, plus their active universe aliases
(`_with_active_aliases`), and it is skipped entirely when nothing is pending. It still
completes before any publication, and a failure for a pending target still blocks the whole
census. `discover` (the discovery CronJob) is unchanged and still advances every active
token. Tests: `tests/unit/test_census_discovery_scope.py`.

## How to avoid / detect

Never judge a backfill Job by its exit code or pod count. Count what it published
(`census_published`) against what it skipped. A retry that publishes 0-1 targets and exits 1
is the signal. Any new per-date step in `census()` that reads warehouse state belongs AFTER
the skip gate, scoped to the pending work. Related: [[backfill-crashes-on-transient-clickhouse-blip]],
[[discovery-wedges-on-unrecognized-getlogs-limit]].
