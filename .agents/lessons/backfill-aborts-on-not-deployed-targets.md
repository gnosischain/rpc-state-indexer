---
name: backfill-aborts-on-not-deployed-targets
symptom: "backfill failed: TOK: ValueError; TOK2: ValueError; ..." at the earliest dates; whole run aborts
area: service
status: active
updated: 2026-07-22
---

## Symptom

A historical `backfill --from <early> --to ...` dies on the very first (earliest) date with a long
list of `ValueError`s — one per token/pool — e.g. `backfill failed: COW: ValueError; sDAI:
ValueError; ...`. The whole multi-year backfill aborts on day one and never progresses.

## Root cause

`IndexerService._active(target, snapshot_date)` originally only gated on `date_start`/`date_end`
(date-based). But **enumerated** tokens/pools carry a `deployment_block` and **no `date_start`**
(`scripts/catalog/enumerate.py` never sets one). So `_active` returned True for every date, and the
census attempted targets at anchors **before they were deployed** → `HistoricalCodeVerifier.verify`
finds no code → raises → the census collects it as a failure → `census` raises `JobRunError` →
`run_backfill` propagated it and aborted the whole range.

Two compounding issues: (1) no block-based deployment guard; (2) `run_backfill` aborted on the first
date with any target failure, so even a single transient failure mid-range killed the run.

## Fix

1. `_active(target, snapshot_date, anchor_block=None)` also returns False when
   `anchor_block < target.deployment_block` — a not-yet-deployed target is **skipped**, not
   attempted. Pass `anchor.number` at every call site (discover / token census / pool census / bench).
2. `run_backfill` catches `JobRunError` per date, emits `backfill_date_failed`, continues, and raises
   a summary at the end — so one bad date never aborts the range (and re-runs skip published pairs via
   the publication gate).

## How to avoid / detect

Any per-date/per-target loop over the catalog must guard on **both** the date window and
`deployment_block` vs the anchor — enumerated targets often lack `date_start`. When adding a backfill
or fan-out over historical dates, verify it *skips* (never *errors on*) targets absent at the anchor,
and that a single target/date failure degrades gracefully instead of aborting the batch. Verified
live: `backfill 2020-07-27..28 --job daily_token_supply` publishes WXDAI and skips the rest. Related:
[[never-coerce-failure-to-zero]], [[catalog-incremental-refresh]].
