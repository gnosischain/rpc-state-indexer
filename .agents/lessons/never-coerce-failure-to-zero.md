---
name: never-coerce-failure-to-zero
symptom: a balance/supply reads 0, or the suspect-zero metric is nonzero
area: census
status: active
updated: 2026-07-21
---

## Symptom

A holder balance, total supply, or pool reserve comes out as `0` when it should have a
value, or `rpc_indexer_census_suspect_zeros_total{token}` is above zero (it is expected to
stay exactly zero).

## Root cause

A failed or missing RPC/decode result was silently coerced to zero — e.g. `value or 0`,
`results.get(addr, 0)`, or `getattr(obj, field, 0)`. This collapses two distinct states:
**observation absent** (we could not read it) and **observed zero** (the chain really
returned 0). The indexer must keep them distinct; absence must fail closed, never publish.

## Fix / correct pattern

On any RPC/decode/code/anchor/subcall failure, raise and let the attempt fail — do not
substitute a default. A successful uint256 is exactly 32 bytes; empty/short/long returns
are failures (`src/rpc_state_indexer/evm/decoding.py`). Failures become `census_errors`
rows and block publication; they are never zero observations.

## How to avoid / detect

`scripts/no_zero_default.py` (run by `make check-fast`) AST-scans
`collectors/`, `core/census.py`, and `evm/decoding.py` and rejects `... or 0`,
`.get(..., 0)`, and `getattr(..., 0)`. Keep that lint green; add failure-branch tests.
Related: [[discovery-must-fail-closed]], [[clickhouse-published-contract]].
