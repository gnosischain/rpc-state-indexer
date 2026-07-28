---
name: observed-sum-overflows-uint256
symptom: "census fails with 'Error serializing column `observed_sum_raw` into data type `Nullable(UInt256)`' / OverflowError: int too big to convert"
area: census
status: fixed
---

## Symptom

A census target fails during publication with a ClickHouse serialization error rather than a
domain error:

```
Error serializing column `observed_sum_raw` into data type `Nullable(UInt256)`
OverflowError: int too big to convert
```

First seen on the ethereum treasury census (2026-07), where targets come from the wallet sweep
rather than a curated catalog.

## Root cause + fix

`observed_sum_raw` is the sum of the scoped holders' balances. Each `balanceOf` return is a valid
uint256, but their **sum is not bounded by uint256** — N holders each near `2**256-1` sum to
roughly `N * 2**256`. Curated catalogs never hit this; auto-discovered spam tokens that mint
~uint256-max to many addresses do.

Worse than the crash: the failure happened *after* `insert_attempt_state(status='verified')` and
before `append_publication`, so the attempt briefly recorded as verified and was only corrected to
`failed` by the outer handler (ReplacingMergeTree resolved it, but the intermediate state was
wrong and the error message carried no domain meaning).

Fix: check the sum against `UINT256_MAX` immediately after computing it and raise
`PublicationBlocked(["observed_sum_overflow"])` — before any verified state is written. Fails
closed with a named reason, publishes nothing, records the attempt as `failed`, and lets the
discovered-target quarantine retire the token after N consecutive failures.

## How to avoid / detect

Any aggregate of on-chain uint256 values needs a width check before it reaches a UInt256 column —
the per-value bound does not imply the aggregate bound. Watch for the same trap in future
sum/product columns (pool reserves, scaled balances). Related:
[[never-coerce-failure-to-zero]] (fail closed, never substitute a value),
[[treasury-sweep-pipeline]] (why unvetted targets reach the census at all).
