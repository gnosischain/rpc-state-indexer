# Architecture and correctness

This document describes the code that is currently implemented. It separates safety
properties enforced by the indexer from downstream comparisons that another system
may choose to build.

## 1. Trust boundary

The indexer's value path has three inputs:

1. a committed YAML catalog and committed ABI fragments;
2. one or more archive JSON-RPC endpoints;
3. ClickHouse for append-oriented persistence and publication views.

There is no dbt client, dbt source, or warehouse-query address selector in the
runtime. This is intentional. If a downstream model infers a balance from logs, the
useful comparison is:

```text
inferred value from analytics pipeline  vs.  direct value from archive RPC
```

Feeding the inferred value into the direct value path would couple their failures.
The current address frame is therefore built only from the indexer's own strict log
scan or from a committed explicit CSV.

Downstream reconciliation is outside this repository. Its safe input boundary is the
set of `v_*_published` views, never the raw attempt or observation tables.

## 2. One snapshot from start to publication

For a token job and snapshot date `D`, the service executes these stages:

1. Load and validate the chain/token/pool/universe/job catalog.
2. Probe every RPC endpoint. Unsafe endpoints are removed from selection.
3. Register the active entities and effective job-target config hashes.
4. Resolve `anchor(D)` from actual block timestamps.
5. For a job using `full_holders`, scan every configured discovery event from the
   token deployment block through `anchor(D)` without gaps.
6. Resolve and freeze the named universe for the token and anchor. Persist the
   members, provenance, ordinal positions, size, and digest under a fresh attempt ID.
7. Verify historical bytecode for the target contract. In the Multicall regime,
   verify Multicall3 against its pinned runtime hash as well.
8. Build calls for every frozen address plus the configured scalar calls.
9. Route each batch to the legacy or Multicall executor according to the anchor block.
10. Strictly decode every result. Persist successful observations and terminal errors
    to separate tables.
11. Read observations and universe membership back from ClickHouse and recompute
    deterministic digests.
12. Check completeness, batch verification, the requested integrity invariant, zero
    terminal errors, and both read-back digests.
13. Append a publication row. Published views inner-join observations to the selected
    publication attempt.

Any target failure is collected and reported without allowing that target to publish.
Other selected targets are still attempted, and the command exits nonzero after the
target loop if any failed.

## 3. UTC day anchors

The snapshot date is UTC. For a date `D`, the anchor is defined as:

```text
highest block whose timestamp < 00:00:00 UTC on D + 1 day
```

`AnchorResolver` does not estimate this block from an average block time. It:

- reads the configured finality tag (`finalized` on Gnosis);
- falls back to `latest - fallback_confirmation_depth` only when the finality tag is
  explicitly unsupported, not when transport simply fails;
- binary-searches actual block timestamps between genesis and the safe tip;
- verifies that the anchor timestamp is before the boundary and the next block's
  timestamp is at or after it;
- verifies that the blocks are adjacent and `next.parentHash == anchor.hash`;
- rereads both boundary blocks after the search to detect a provider changing forks
  during resolution.

Every resolution is appended to `day_anchors`. `v_day_anchors_canonical` includes a
date only when all recorded resolutions agree and were finalized. A second distinct
resolution appears in `v_anchor_conflicts`, and publications at that date become
ineligible.

## 4. Gap-free holder discovery

Each token declares its discovery events and which indexed topics contain holder
addresses. For example, WXDAI declares `Transfer`, `Deposit`, and `Withdrawal`; WETH
declares only `Transfer`. The event-to-topic mapping is validated against committed
ABI fragments during configuration validation.

Discovery uses half-open ranges `[start, end)` internally and performs a strict
`eth_getLogs` call for every range. It has four fail-closed behaviors:

- Provider-limit errors split a range in half. A permanently failing one-block range
  fails the scan; it is never skipped.
- A result count equal to the configured provider cap is treated as possible silent
  truncation and split. Hitting the cap on one block fails the scan.
- Every returned log is checked for contract address, topic, canonical block and
  transaction metadata, in-range block number, non-removed status, and canonical
  indexed address words.
- Completed ranges are unioned and checked for uncovered gaps before the scan can
  return.

The persistence order matters:

```text
per-block log counts -> holder observations -> completed range marker
```

If the process crashes, a retry may append duplicate mergeable observations, but it
cannot claim a completed discovery range before dependent rows were accepted.
Coverage is measured directly from the completed half-open ranges in
`discovery_ranges`, anchored at each token's configured deployment block, so a
missing prefix cannot masquerade as complete coverage.

The holder universe excludes the all-zero event sentinel. A legitimate observed zero
balance for a real holder remains in the dense census output.

## 5. Address universes

A universe is resolved independently for each token and anchor. Implemented selector
kinds are:

| Kind | Membership |
|---|---|
| `full_holders` | Every address found by this indexer's configured event scan at or before the anchor, plus configured `seed_holders` |
| `explicit_list` | Addresses in a committed CSV selected by the YAML catalog |
| `union` | Set union of two or more named universes |
| `intersect` | Set intersection of two or more named universes |

Resolution produces sorted unique addresses and provenance labels. Both are hashed
and persisted for the attempt. The source CSV's file hash is part of the effective
config hash, so changing a vendored list invalidates eligibility of publications made
under the previous definition.

`full_supply` and `scaled_full_supply` jobs are currently required to use the universe
named `full_holders` exactly. Scoped watchlists use `integrity_mode: scoped` and make no
claim about addresses outside the list.

## 6. Historical execution routing

The router selects solely from the pinned anchor block:

```python
if anchor.number >= multicall_deployment_block:
    executor = Multicall3Executor
else:
    executor = LegacyRpcBatchExecutor
```

Both executors return the same `VerifiedBatchResult` contract: ordered call results
plus executor kind, block-reference kind, anchor hash, provider groups, result digest,
and a verification flag.

### 6.1 Before Multicall3: verified JSON-RPC batching

The legacy executor is the mechanism that includes pre-Multicall3 history. It first
tries an endpoint that passed the EIP-1898 probe. Each `eth_call` uses:

```json
{
  "blockHash": "0x<anchor hash>",
  "requireCanonical": true
}
```

If an endpoint supports HTTP JSON-RPC batches, the calls are sent as one batch and
decoded by response ID; response order is not trusted. Missing, duplicate, unknown,
or malformed response IDs fail the batch. If HTTP batching is unavailable, calls are
sent concurrently one at a time with the same block reference.

If no usable EIP-1898 endpoint remains, the fallback requires the configured number
of distinct provider groups. For every provider it performs:

```text
read anchor block hash
execute number-pinned historical calls
read anchor block hash again
```

Both hashes must equal the persisted anchor, and the complete raw-result digest must
match across provider groups. Disagreement raises `ProviderQuorumMismatch`; no value
is accepted.

Provider-limit errors cause adaptive call-batch splitting. Other malformed responses
or exhausted safety paths fail.

### 6.2 At and after Multicall3: aggregate3 plus sentinels

Each Multicall3 body is wrapped with the following calls at both head and tail:

| Sentinel | Expected result |
|---|---|
| `getBlockNumber()` | anchor block number |
| `getCurrentBlockTimestamp()` | anchor block timestamp |
| `getBlockHash(anchor - 1)` | anchor parent hash |

All six sentinel calls use `allowFailure=false`. The executor requires the decoded
result count to equal the packed call count exactly and verifies both sentinel sets.
This detects a proxy silently serving `latest`, a wrong block reference, a changed
fork view, and tail starvation that would otherwise look like ordinary failed
subcalls.

Failed body subcalls are retried as single-call Multicall batches with the same
sentinels. A provider-limit error adaptively halves the batch. A result is not marked
verified until the sentinels and result shape pass.

The underlying `eth_call` prefers EIP-1898. When an endpoint lacks it, the Multicall
executor uses a number reference with an anchor-hash check immediately before and
after the aggregate call.

### 6.3 Endpoint capability probe

Startup probes every endpoint without printing its URL. A passing endpoint has:

- the configured chain ID;
- valid block data for the finality tag, when supported;
- archive bytecode and a strict state call at the earliest enabled token deployment;
- the configured Multicall3 runtime bytecode hash at that block;
- an HTTP batch capability flag;
- an EIP-1898 verdict based on both a successful canonical block-hash call and a
  bogus-hash call that must fail.

The positive EIP-1898 call invokes Multicall3 `getBlockNumber()` and requires the
returned number to equal the requested deployment block. A proxy that ignores the
hash and serves `latest` therefore fails the probe.

The current probe's `archive_from_block` evidence is the earliest enabled token
deployment. It verifies historical bytecode and a strictly decoded state call there,
independently of the Multicall codehash probe. Large backfills should still be
benchmarked at their oldest intended anchor on the production endpoint tier.

## 7. Strict observation decoding

State observations are typed as success with an integer or failure with no value.
For uint256 functions, successful return data must be exactly 32 bytes. In
particular:

- empty return data from a codeless address is not zero;
- a reverted subcall is not zero;
- malformed, short, or long data is not zero;
- an archive/pruned-state error is not zero;
- a transport retry exhaustion is not zero.

Before collection, `HistoricalCodeVerifier` requires non-empty bytecode at the pinned
anchor. The Multicall runtime is additionally checked against its committed keccak
hash.

Successful `balance_raw = 0` is persisted like any other direct observation. Failure
rows go to `census_errors` and do not create rows in `token_balances` or
`pool_token_balances`.

## 8. Collectors and invariants

### ERC-20

For every address in the frozen universe, the collector reads `balanceOf(address)`.
It also reads each configured supply scalar. The starter standard-token jobs use
`totalSupply`.

- `full_supply` requires every call to succeed and exact
  `sum(balanceOf(holder)) == totalSupply()`.
- `scoped` requires complete observations over the selected universe but deliberately
  does not assert a supply invariant.

The exact full-supply check means the catalog/event frame must genuinely cover the
entire holder universe for that token. A token with unusual holder creation or supply
semantics must declare the relevant discovery events or use a scoped job until its
invariant is understood.

### Aave/Spark aTokens

For each holder the collector reads `scaledBalanceOf`. It also reads configured token
scalars, including `scaledTotalSupply`, and calls the configured pool's
`getReserveNormalizedIncome(reserve)`.

The displayed balance is reconstructed with exact positive half-up ray multiplication:

```text
(scaled_balance * liquidity_index + 5 * 10^26) // 10^27
```

`scaled_full_supply` requires exact
`sum(scaledBalanceOf(holder)) == scaledTotalSupply()`. No floating-point arithmetic
is used.

### Pool reserves

For each configured pool asset, the collector calls the asset token's
`balanceOf(pool_address)`. `pool_assets` requires every configured asset observation
to succeed. This is a direct token-balance observation; the current implementation
does not read Uniswap `slot0`, Algebra global state, or LP accounting events.

## 9. Attempt and publication protocol

Every run for a target/date creates a new UUID attempt. Observation sort keys include
`attempt_id`, so a failed retry cannot overwrite a published attempt.

Before appending a publication, the census runner requires:

- historical target bytecode verification;
- zero persisted terminal errors for the attempt;
- every returned batch marked verified;
- collector observation completeness;
- the configured integrity check (`full_supply`, `scaled_full_supply`, `scoped`, or
  `pool_assets`);
- a ClickHouse read-back observation digest equal to the in-memory result digest;
- for token jobs, a read-back universe digest equal to the frozen universe digest.

Only after those checks pass does it append `census_publications`. A failure updates
the attempt state to `failed` and raises; it does not append a publication.

Publication eligibility is stricter than row existence:

1. the publication config hash must equal the currently registered config hash;
2. its anchor block and hash must equal the canonical anchor for the date;
3. all eligible rows for a `(chain, job, target, date)` must have one signature made
   from anchor hash, config hash, universe hash, and result digest.

If multiple eligible publications have the same signature, the latest attempt is
selected. If signatures disagree, the key appears in `v_publication_conflicts` and is
excluded from `v_publications_current`. Published observation views inner-join on the
selected attempt ID.

A reconciliation mismatch with an external dataset is not a publication check. A
publication proves that this RPC observation is internally complete and pinned; it
does not require another system to agree.

## 10. ClickHouse table roles

| Area | Tables/views | Role |
|---|---|---|
| Registry | `config_registry`, `v_config_registry_current` | Effective per-target config contract (entity window folded into `config_registry`) |
| Anchors | `day_anchors`, `v_day_anchors_canonical`, `v_anchor_conflicts` | Immutable UTC-boundary evidence |
| Discovery | `discovery_ranges`, `holder_universe` | Gap-free log coverage and own holder census |
| Attempts | `census_attempts` (batch evidence folded into `batches_json`), `census_universe_members`, `census_errors` | Diagnostic execution ledger |
| Values | `token_balances`, `token_scalars`, `pool_token_balances` | Exact attempt-scoped observations |
| CL primitives | `pool_cl_state`, `pool_tick_liquidity` | Concentrated-liquidity state + per-tick primitives (signed `liquidity_net`) |
| Gate | `census_publications`, `v_publications_current`, `v_*_published` | Consumer-safe publication contract |
| Derived (compute) | `pool_liquidity_profile`, `v_pool_liquidity_profile` | Layer-2 recomputed metrics; provenance to a published attempt, not behind the RPC gate |
| Operations | `writer_heartbeats` | Single-writer coordination |

Amounts use ClickHouse `UInt256` (`Int256` where genuinely signed, e.g. `liquidity_net`).
Addresses are lowercase `0x`-prefixed `String` values. `chain_id` is the first key dimension
throughout the domain tables and views.

## 11. Two layers: verified ingestion vs. derived compute

Ingestion collectors (`collectors/`, incl. `cl_liquidity`) land raw on-chain primitives
pinned to an anchor, sentinel-verified and read-back-digested, published behind `v_*_published`
— no math. Compute modules (`compute/`, e.g. `cl_profile`) read only those published views and
write derived tables, deterministically and RPC-free, each row carrying provenance
(`source_attempt_id` + `source_result_digest`) back to the verified snapshot. A new contract is
a new collector; a new metric is a new compute module. Run compute with `compute --date <d>`;
it needs the ClickHouse repository only (no RPC runtime, no writer heartbeat) and is idempotent.

ReplacingMergeTree tables must be read with their current views or `FINAL` where
deduplication matters. The committed published views already do this.

## 11. Process and chain model

The configuration and schema are chain-aware, but the runtime intentionally runs one
selected chain per process (`CHAIN=gnosis`). There is no cross-chain scheduler or
aggregation layer.

A coarse ClickHouse heartbeat guard permits one writer per chain. A fresh competing
heartbeat causes an execution command to refuse startup. There is currently no force
override flag. Read-only `status`, `validate`, and non-persisted `probe` operations do
not acquire this writer service guard.

## 12. Guarantees versus current limitations

Enforced now:

- exact finalized day anchors;
- no skipped discovery block;
- automatic legacy/Multicall routing;
- hash-pinned or quorum-verified historical calls;
- no error-to-zero conversion;
- dense observed zeros;
- attempt isolation and publication gating;
- exact integer integrity checks and read-back digests.

Not implemented yet:

- a full production entity catalog or catalog generator;
- dbt/warehouse-derived holder selectors;
- publication contiguity as a pre-publication check;
- a repair-request worker or `repair` CLI;
- native-coin balances;
- downstream inferred-vs-direct reconciliation;
- cross-chain identity or aggregation.

Operational validation does report anchor conflicts, publication conflicts, log-count
conflicts, unfinished attempts, unrepaired failed attempts, and unresolved errors.
