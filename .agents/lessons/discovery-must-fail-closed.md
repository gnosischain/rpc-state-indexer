---
name: discovery-must-fail-closed
symptom: discovery gap, a log range was skipped, or holder universe is incomplete
area: discovery
status: active
updated: 2026-07-21
---

## Symptom

The holder universe is missing addresses, or coverage advances past a range that never
actually succeeded — a silent gap in `eth_getLogs` discovery.

## Root cause

A failing log range or block was swallowed instead of stopping the scan — e.g.
`except ...: pass`, a bare `continue` with no fail-closed `raise`, or a "failed, skipped"
warning that lets coverage move on. Discovery must be **gap-free**: a permanently failing
block stops the scan, and coverage never advances past an unproven range.

## Fix / correct pattern

In `src/rpc_state_indexer/core/discovery.py`, on a range/block failure, raise and exit
nonzero without recording the range as complete. Adaptive range splitting handles
provider result caps; a genuine failure still stops. Rerun after fixing provider
availability — completed half-open ranges are reused, only gaps are re-requested.

## How to avoid / detect

`scripts/no_silent_rpc_failures.py` (run by `make check-fast`) AST-scans `discovery.py`
and fails on `except: pass`, on `continue` without a fail-closed `raise`, and on skip
markers like `"failed, skipped"` / `"warn: block"`. Keep it green.
Related: [[never-coerce-failure-to-zero]].
