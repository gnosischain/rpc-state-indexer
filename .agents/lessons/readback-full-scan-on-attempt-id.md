---
name: readback-full-scan-on-attempt-id
symptom: "census/backfill gets slower as history grows; each target takes ~10s+ with clean batches, 0 failures, and healthy RPC/ClickHouse latency"
area: storage
status: fixed
---

## Symptom

Census attempts take ~11s each despite:
- one Multicall3 batch of ~24 calls per attempt (`batches_total = 1`),
- `observations_failed = 0` (no retries),
- ClickHouse round-trip 64ms, RPC round-trip 92ms, single `eth_call` 108ms.

The per-attempt time also *grows* over the life of the deployment, so a long backfill decelerates.

## Root cause + fix

Every read-back filtered on `attempt_id` alone:

```sql
SELECT ... FROM token_balances FINAL WHERE attempt_id = {attempt_id:UUID}
```

`attempt_id` is the **5th** column of `ORDER BY (chain_id, job_name, token_address, snapshot_date,
attempt_id, holder_address)`. A predicate on it alone cannot use the sort-key prefix, so ClickHouse
full-scans — with `FINAL`, forcing a merge across all parts. Measured on `token_balances` at
**208M rows**:

| query | time |
|---|---|
| `WHERE attempt_id = …` | **8,701 ms** |
| `WHERE chain_id/job_name/token_address/snapshot_date/attempt_id = …` | **62 ms** |

**141x.** `census_universe_members` (219M rows) cost a further **6,698 ms** the same way, so a single
token attempt paid ~15s of scan before doing any real work — and both tables grow with every row
the indexer writes, so a long backfill decelerates itself.

Seven queries across five methods were affected (`terminal_error_count`,
`readback_universe_digest`, `readback_token_digest` (x2), `readback_pool_digest`,
`readback_cl_digest` (x2)) — and each attempt runs several of them, so the publication path paid
this repeatedly per target.

Fix: `AttemptScope` (chain_id, job_name, target_address, snapshot_date, attempt_id) carries the
sort-key prefix; `AttemptScope.predicate(address_column)` builds the WHERE clause. All read-backs
take a scope, built from the attempt `base` row via `CensusRunner._scope()`.

## How to avoid / detect

`attempt_id` is a UUID and *looks* like a primary key — it is not; it is a late column in a
compound sort key. Any query filtering by it must also carry the prefix.
`tests/unit/test_repository_schema.py::test_attempt_scoped_reads_use_the_sort_key_prefix` fails the
build if `WHERE attempt_id = {{attempt_id:UUID}}` reappears. When a job is unexpectedly slow,
compare per-attempt wall time against measured RPC + ClickHouse round-trips before blaming the
provider — a gap of 10x+ with clean batches points at a scan, not the network.
