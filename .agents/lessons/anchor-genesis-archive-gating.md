---
name: anchor-genesis-archive-gating
symptom: census fails "eth_getBlockByNumber exhausted RPC endpoints" / RpcNoHealthyEndpoint, even with a healthy archive endpoint
area: rpc
status: resolved
updated: 2026-07-21
---

## Symptom

A census on a valid recent date dies at anchor resolution:
`RpcAttemptsExhausted: eth_getBlockByNumber exhausted RPC endpoints` ->
`RpcNoHealthyEndpoint: no RPC endpoint can serve the request` — even though the endpoint
passed the startup safety probe and can read old state fine.

## Root cause

The startup capability probe sets `endpoint.archive_from_block` to the earliest configured
token's deployment block (WXDAI 11,173,937) — the only historical point it proves. But the
anchor resolver (`core/anchors.py:resolve`) reads **block 0 (genesis)** to bound its binary
search: `genesis = await read(0)`. That goes through `_read_by_number(0)`, which passed
`historical_block=0` into endpoint selection. `RpcEndpoint.can_serve(0)` returns False
because `0 < archive_from_block`, so no endpoint qualifies — a block *header* read is
rejected by the *state*-archive floor.

## Fix / correct pattern

Block *header* reads (`eth_getBlockByNumber`) are available on any full node and must not be
gated by `archive_from_block` (which represents historical *state* availability, for
`eth_call`/`eth_getCode`). Fixed by not passing `historical_block` from `_read_by_number`.
After the fix, census resolves the anchor and publishes normally.

## How to avoid / detect

Distinguish header reads from state reads when applying archive gating. Any change to
`archive_from_block` semantics or the anchor binary search should be tested with an endpoint
whose `archive_from_block` is above 0. Real error was masked by the CLI —
see [[cli-hides-real-db-error]] (same technique: unwrap `__cause__` via a local repro).
