---
name: discovery-wedges-on-unrecognized-getlogs-limit
symptom: "backfill_date_failed failure_count:N on every date after some point; token_scalars stop advancing; census_errors empty"
area: rpc / discovery
status: active
updated: 2026-07-23
---

## Symptom

A historical backfill publishes fine up to some date, then **every** later date fails with a flat
`backfill_date_failed failure_count:N` (N = a fixed set of high-volume tokens), and `token_scalars`
/ publications stop advancing at that date. `census_errors` is **empty** (misleading — suggests no
error). The stuck tokens are always the busiest ones (WXDAI, WETH, USDC, sDAI on Gnosis).

## Root cause

Full-holder **discovery** (`eth_getLogs`), not the state-call census, is failing — so the errors
land in `discovery_ranges` (`status='failed'`), not `census_errors`. For a dense token, a
10,000-block (`initial_chunk_size`) log window exceeds the provider's result cap. The provider
(Reth/Erigon) rejects it with `-32602 "query exceeds max results 20000, retry with the range …"`.

That phrase matched **none** of `classification._LIMIT_MARKERS`, so it classified as `PERMANENT`
instead of `PROVIDER_LIMIT`, never normalized to `RpcProviderLimit`, and the discovery scanner's
adaptive-split path ([`core/discovery.py`](../../src/rpc_state_indexer/core/discovery.py), the
`except RpcProviderLimit` at the read site) never triggered. Instead the read fell to the scanner's
generic `except Exception` branch, which raises `DiscoveryRangeFailed("RPC failed for [a,b);
coverage was not advanced")` **without subdividing** — so coverage wedges at that chunk forever, and
every subsequent date re-hits the same wall.

Two diagnosability gaps hid the cause: (1) `discovery_service` persisted only the wrapper message,
not `exc.__cause__` (the real provider text); (2) `service.discover()` swallowed the exception into
a bare `failure_count` with no log line.

## Fix

Classification is the right layer (the split machinery already exists). In
[`rpc/classification.py`](../../src/rpc_state_indexer/rpc/classification.py):
- Added markers `exceeds max results`, `too many results`, `too many logs`, `max logs per
  response`, `please limit`, `request timed out` (mirrors cow-indexer's `is_range_error`).
- `_message()` now also scans the JSON-RPC **`data`** field — some providers keep `message`
  generic ("invalid params") and put the reason in `data`.
- Added `_LIMIT_CODES = {-32005, -32016}` (code-based, message-independent). `-32602` is
  deliberately NOT a limit code — it is generic and only a range signal when a marker appears.

Once classified as `PROVIDER_LIMIT`, the client normalizes to `RpcProviderLimit` (not-retryable,
not-failover → raised straight through), the scanner catches it and halves the range until it fits.
Diagnosability: `discovery_service` now persists `__cause__`; `service.discover()` emits
`discovery_failed{token,error,detail}`. Tests: `tests/unit/test_classification.py`.

## How to avoid / detect

When a backfill/discovery fails, query `discovery_ranges FINAL WHERE status='failed'` (NOT
`census_errors`) — `error_class` + `error_message` carry the reason. A new provider's limit phrase
will wedge discovery the same way until added as a marker; the `discovery_failed` log + persisted
`__cause__` now make that a one-line fix instead of a spelunk. Any new archive endpoint should be
probed against a dense-token 10k-block `eth_getLogs` before relying on it. Related:
[[discovery-must-fail-closed]], [[gnosis-public-archive-eip2930]].
