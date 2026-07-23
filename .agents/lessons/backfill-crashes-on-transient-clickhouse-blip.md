---
name: backfill-crashes-on-transient-clickhouse-blip
symptom: "backfill failed (DatabaseError); ingest stops mid-range; service_cleanup_failed writer_guard; then the pod grinds the whole compute loop"
area: service / storage
status: active
updated: 2026-07-23
---

## Symptom

A long `backfill` stops advancing at some date (e.g. all token jobs frozen at 2021-03-04), the pod
log ends the ingest phase with `error: backfill failed (DatabaseError)` and
`service_cleanup_failed: {writer_guard: ServiceError}`, then spends hours in the shell compute loop
(`compute_complete … rows:0`) before exiting non-zero and being retried by the Job — re-hitting the
same wall. `census_errors` and `discovery_ranges` show nothing new (the crash wasn't a per-target
failure).

## Root cause

Two layers:
1. **External, transient**: ClickHouse Cloud restarted/replaced a server node (visible in
   `system.query_log` as `operator-internal` `Code 210 Broken pipe` / `Code 279 "All connection
   tries failed … Connection refused (c-…-server-…)"` around the crash time). A census write in
   flight hit the dead socket and surfaced as a generic `DatabaseError`.
2. **Our bug**: `run_backfill` only caught `JobRunError` (per-target census/discovery failures).
   Any *other* exception — a transient `DatabaseError`, an anchor-resolution RPC error — propagated
   out of the per-date loop, crashed the whole multi-year ingest, and (because the shell runs
   `compute` unconditionally after `backfill`) still burned hours in the compute loop before the
   Job failed and retried into the same crash.

The `Code 62 "Max query size exceeded"` also seen in the log was a **different** service
(`cow_user` DELETE on `cow_db`) on the same shared instance — a red herring, not ours.

## Fix

`run_backfill` now also catches any non-`JobRunError` per date, records it, emits
`backfill_date_failed{error,detail}`, and continues; the summary `ServiceError` is still raised at
the end. So a routine CH Cloud node cycle fails at most the few dates in the outage window instead
of the whole run, and a re-run fills them via the publication gate. Test:
`test_backfill_survives_transient_error_on_one_date`.

Follow-up (not yet done): a bounded retry-on-transient wrapper around the repository
`insert_rows`/`query_rows` (retry on `Code 210`/`279`/timeouts with backoff) would absorb the blip
entirely so no dates fail. CH Cloud restarts nodes routinely, so this is worth adding.

## How to avoid / detect

When a backfill dies with an opaque `DatabaseError`, check the CH instance's own
`system.query_log` (filter OUT other users like `user_max`/`cow_user` and `Code 497`) for
`operator-internal` connection errors around that time — a node restart looks like `Code 210/279`.
Any long-running writer against CH Cloud must treat a single write failure as a skippable/retryable
event, never as fatal to the whole run. Related: [[cli-hides-real-db-error]],
[[writer-heartbeat-refusal]].
