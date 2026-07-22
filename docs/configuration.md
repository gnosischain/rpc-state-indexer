# Configuration guide

The configuration boundary is intentionally small:

- YAML and committed ABI/CSV files define the domain to index;
- environment variables define endpoints, credentials, concurrency, batch sizes, and
  process behavior;
- there is no runtime explorer lookup, dbt lookup, Jinja expansion, Python expression,
  or arbitrary SQL address selector.

All runtime addresses are normalized to lowercase `0x` strings. Amounts remain raw
integers; decimals are metadata and are never applied to persisted values.

## 1. File loading

With `CHAIN=gnosis` and `CONFIG_ROOT=config`, the loader reads exactly:

```text
config/chains.yaml
config/gnosis/tokens.yaml
config/gnosis/pools.yaml
config/gnosis/universes.yaml
config/gnosis/jobs.yaml
```

ABI names in token event definitions resolve to `ABI_ROOT/<name>.json`. Explicit-list
sources resolve relative to the chain directory, for example
`config/gnosis/vendored/treasury_addresses.csv`.

Run the offline validator after every change:

```bash
rpc-state-indexer validate-config
```

It loads Pydantic contracts, checks cross-references and job semantics, detects
universe cycles, and validates configured event/topic positions against the ABI.

## 2. Chain catalog

`config/chains.yaml` is keyed by the runtime `CHAIN` value:

```yaml
chains:
  gnosis:
    chain_id: 100
    finality_tag: finalized
    fallback_confirmation_depth: 64
    expected_block_time_seconds: 5

    multicall3:
      address: "0xca11bde05977b3631167028862be2a173976ca11"
      deployment_block: 21022491
      runtime_code_hash: "0xd5c15df687b16f2ff992fc8d767b4216323184a2bbc6ee2f9c398c318e770891"
      default_batch_size: 250

    legacy_execution:
      enabled: true
      default_batch_size: 100
      preferred_block_reference: eip1898
      number_fallback:
        enabled: true
        required_provider_quorum: 2
        require_distinct_provider_groups: true
        hash_sandwich: true

    discovery:
      initial_chunk_size: 10000
      provider_result_cap: 10000
```

Field behavior:

| Field | Runtime meaning |
|---|---|
| `chain_id` | Required response from every RPC endpoint and first persisted key dimension |
| `finality_tag` | Preferred safe tip used when resolving day anchors |
| `fallback_confirmation_depth` | Used only when the finality tag is explicitly unsupported |
| `expected_block_time_seconds` | Chain metadata; anchor resolution still uses actual block timestamps |
| `multicall3.address` | Contract called for post-deployment batching and sentinels |
| `multicall3.deployment_block` | Exact automatic routing boundary |
| `multicall3.runtime_code_hash` | Required keccak hash of runtime bytecode at historical anchors |
| `legacy_execution.*` | Safety contract for anchors below the Multicall deployment block |
| `discovery.initial_chunk_size` | Initial `eth_getLogs` half-open range size |
| `discovery.provider_result_cap` | Equal-to-cap responses are treated as possible truncation |

The YAML default batch-size fields are catalog metadata. The currently constructed
executors take their active sizes from `MULTICALL_BATCH_SIZE` and
`LEGACY_RPC_BATCH_SIZE` environment values.

### Adding another chain

The schema and runtime are chain-aware, but one process indexes one chain. To add a
chain:

1. Add a key under `chains` with an independently verified chain ID, finality policy,
   Multicall address, deployment block, and runtime bytecode hash.
2. Add `config/<chain>/tokens.yaml`, `pools.yaml`, `universes.yaml`, and `jobs.yaml`.
3. Add any chain-specific committed ABI fragments or vendored CSVs.
4. Run `rpc-state-indexer validate-config --chain <chain>`.
5. Probe archive RPCs with `CHAIN=<chain>` before any census.
6. Run a separate process and endpoint pool for that chain.

The current pinned-chain test is Gnosis-specific; add equivalent fixed-block evidence
for a new chain.

## 3. Token catalog

Each row in `tokens.yaml` defines contract lifetime, collection behavior, and holder
discovery behavior.

### Standard ERC-20 example

```yaml
tokens:
  - address: "0x6a023ccd1ff6f2045c3309768ead9e68f978f6e1"
    symbol: WETH
    decimals: 18
    token_class: standard_erc20
    deployment_block: 11568333
    date_start: 2020-08-19
    balance_function: balanceOf
    supply_functions: [totalSupply]
    discovery_events:
      - abi: erc20
        event: Transfer
        holder_topics: [1, 2]
```

For the committed ERC-20 ABI, topic 1 is `from` and topic 2 is `to`. The indexer scans
the event from `deployment_block`, extracts both addresses, and drops the all-zero
event sentinel.

### WETH9-style token example

Contracts do not necessarily emit the same events merely because their interfaces
look similar. WXDAI explicitly includes its non-Transfer holder-changing events:

```yaml
- address: "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"
  symbol: WXDAI
  decimals: 18
  token_class: weth9_fork
  deployment_block: 11173937
  date_start: 2020-07-27
  balance_function: balanceOf
  supply_functions: [totalSupply]
  discovery_events:
    - {abi: erc20, event: Transfer, holder_topics: [1, 2]}
    - {abi: weth9, event: Deposit, holder_topics: [1]}
    - {abi: weth9, event: Withdrawal, holder_topics: [1]}
```

Do not copy this event set to another token without verifying its ABI and emitted logs.

### Aave/Spark aToken example

```yaml
- address: "0xd0dd6cef72143e22cced4867eb0d5f2328715533"
  symbol: aGnoWXDAI
  decimals: 18
  token_class: aave_v3_atoken
  deployment_block: 30834029
  date_start: 2023-11-07
  balance_function: scaledBalanceOf
  supply_functions: [totalSupply, scaledTotalSupply]
  index_source:
    contract: "0xb50201558b00496a145fe76f7424749556e326d8"
    function: getReserveNormalizedIncome
    argument: "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"
    output_name: liquidity_index_ray
  discovery_events:
    - {abi: erc20, event: Transfer, holder_topics: [1, 2]}
```

An aToken must use `scaledBalanceOf`, include `scaledTotalSupply`, and declare an index
source. The implemented token classes are:

- `standard_erc20`
- `weth9_fork`
- `aave_v3_atoken`
- `spark_atoken`

The implemented uint256 supply calls are `totalSupply` and `scaledTotalSupply`. Do not
add another `supply_functions` name until its calldata implementation and tests exist.

### Lifetime fields

`deployment_block` is the first block the event scan considers. `date_start` and
`date_end` gate snapshots:

```text
date_start <= snapshot_date < date_end
```

`date_end` is exclusive. Disabled targets (`enabled: false`) are omitted by class and
`all_enabled` selectors. Address selectors must still reference catalog entries.

Before committing a target, verify at minimum:

- deployment transaction/block;
- non-empty bytecode at and after the first intended anchor;
- decimals and token class;
- every holder-changing event and indexed address position;
- supply function semantics;
- proxy or address lifetime windows.

## 4. Pool catalog

The current pool collector reads direct token balances. A pool row names the target
contract and lists every token that should be called:

```yaml
pools:
  - address: "0x4a562e482e9e6b140b322ca50cc4d8535cdf85c9"
    name: "WETH-WXDAI Uniswap V3"
    pool_class: uniswap_v3
    deployment_block: 35864026
    date_start: 2024-09-06
    assets:
      - token: "0x6a023ccd1ff6f2045c3309768ead9e68f978f6e1"
      - token: "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"
```

Every asset token must also exist in `tokens.yaml`. For this example the state plan is:

```text
WETH.balanceOf(pool)
WXDAI.balanceOf(pool)
```

`pool_class` is persisted metadata; it does not currently select a specialized pool
ABI reader. Pool `date_end` is exclusive, just like token `date_end`.

## 5. Universe catalog

Universes are named and reusable. The runtime freezes a separate membership for each
token, anchor, and attempt.

### Full holder universe

```yaml
universes:
  full_holders:
    kind: full_holders
```

This queries the indexer's `holder_universe` table for addresses discovered at or
before the anchor. It does not query any dbt table.

### Explicit list

```yaml
universes:
  treasury:
    kind: explicit_list
    source: vendored/treasury_addresses.csv
    address_column: address
```

CSV:

```csv
address
0x1111111111111111111111111111111111111111
0x2222222222222222222222222222222222222222
```

The CSV must be present in the repository at runtime. Values are normalized, sorted,
and deduplicated. Its SHA-256 file hash is included in every referencing target's
effective config hash.

### Union and intersection

```yaml
universes:
  full_holders:
    kind: full_holders

  treasury:
    kind: explicit_list
    source: vendored/treasury_addresses.csv

  full_plus_treasury:
    kind: union
    of: [full_holders, treasury]

  known_treasury_holders:
    kind: intersect
    of: [full_holders, treasury]
```

`union` and `intersect` require at least two named children. References may nest;
cycles are rejected.

Currently absent selector kinds include `dbt_claimed_nonzero`, `warehouse_query`, and
`event_touched`. Adding one requires a typed config contract, deterministic provenance
and hashing, tests, and a conscious decision about whether it weakens source
independence.

## 6. Job catalog

A job combines target selection, address scope, cadence, and integrity semantics.

### Full ERC-20 census

```yaml
jobs:
  daily_erc20_full:
    target_kind: tokens
    token_selector:
      class_in: [standard_erc20, weth9_fork]
    universe: full_holders
    cadence: daily
    integrity_mode: full_supply
    coverage_start: null
```

### Scoped addresses across all tokens

```yaml
jobs:
  daily_treasury:
    target_kind: tokens
    token_selector:
      all_enabled: true
    universe: treasury
    cadence: daily
    integrity_mode: scoped
```

### Pool reserves

```yaml
jobs:
  daily_pool_reserves:
    target_kind: pools
    pool_selector:
      all_enabled: true
    cadence: daily
    integrity_mode: pool_assets
```

Token selectors must choose exactly one of:

```yaml
token_selector: {all_enabled: true}
token_selector: {class_in: [standard_erc20, weth9_fork]}
token_selector: {addresses: ["0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"]}
```

Pool selectors must choose exactly one of:

```yaml
pool_selector: {all_enabled: true}
pool_selector: {addresses: ["0x4a562e482e9e6b140b322ca50cc4d8535cdf85c9"]}
```

Integrity modes:

| Mode | Allowed target | Publication invariant |
|---|---|---|
| `full_supply` | non-aToken token job | All calls succeed and holder sum equals `totalSupply` exactly |
| `scaled_full_supply` | aToken job | All calls succeed and scaled holder sum equals `scaledTotalSupply` exactly |
| `scoped` | token job | All calls in the selected universe succeed; no global supply claim |
| `pool_assets` | pool job | Every configured asset balance call succeeds |

The loader currently requires exact-supply jobs to reference the universe named
`full_holders`, not a union alias. aTokens cannot use `full_supply`, and non-aTokens
cannot use `scaled_full_supply`.

`cadence: daily` makes the daemon run the job for yesterday. `cadence: manual` leaves
it available to explicit `census`, `backfill`, and `densify` commands but skips it in
the daemon. `coverage_start` influences the coverage-calendar view; it does not launch
a backfill automatically.

## 7. Effective config hash

For each job-target pair the service hashes canonical JSON containing:

- the state/correctness-relevant chain config;
- the job config except `cadence`;
- the target token or pool config;
- the recursively expanded universe definition;
- SHA-256 hashes of referenced vendored files.

The chain hash deliberately excludes throughput-only
`expected_block_time_seconds`, `discovery`, and both catalog default batch sizes.
Those fields can be tuned without changing the meaning of an already observed
snapshot. `cadence` is likewise scheduling-only and is excluded.

The resulting 64-character SHA-256 is persisted in `config_registry`, attempts, and
publications. Published views accept only the hash currently registered for that
job-target. A material catalog change therefore makes the prior publication
ineligible and permits a new attempt for the same date.

Operational environment values such as endpoint URLs, concurrency, and active batch
sizes are not part of this catalog hash. Execution evidence records executor/reference
and provider groups separately.

## 8. Runtime environment

`.env.example` contains every setting. The important groups are:

| Variable | Default | Meaning |
|---|---:|---|
| `CHAIN` | `gnosis` | Chain config/directory selected by this process |
| `CONFIG_ROOT` | `config` | YAML root |
| `ABI_ROOT` | `abis` | Committed ABI root |
| `MIGRATIONS_DIR` | `migrations` | Numbered SQL migrations |
| `RPC_URLS` | required for RPC commands | Comma-separated archive endpoints |
| `RPC_PROVIDER_GROUPS` | all `unclassified` if absent | One real operator group per URL; unlabeled URLs cannot form a legacy quorum |
| `CLICKHOUSE_HOST` | required for storage commands | ClickHouse host only, without scheme |
| `CLICKHOUSE_PORT` | `8443` | ClickHouse HTTP(S) port |
| `CLICKHOUSE_USER` | `default` | ClickHouse username |
| `CLICKHOUSE_PASSWORD` | empty | Secret password |
| `CLICKHOUSE_DATABASE` | `rpc_indexer` | Database created/queried by the service |
| `CLICKHOUSE_SECURE` | `true` | TLS transport |
| `CLICKHOUSE_VERIFY` | `true` | TLS certificate verification |
| `RPC_CONCURRENCY` | `8` | Global in-flight RPC semaphore |
| `RPC_REQUESTS_PER_SECOND` | `30` | Per-endpoint request limit |
| `MULTICALL_BATCH_SIZE` | `250` | Initial post-deployment body call chunk |
| `LEGACY_RPC_BATCH_SIZE` | `100` | Initial pre-deployment call chunk |
| `MAX_RETRIES` | `5` | Transport/executor retry budget |
| `WRITER_STALE_SECONDS` | `120` | Fresh-heartbeat overlap window |
| `METRICS_PORT` | `9090` | Daemon HTTP listener |
| `DAEMON_POLL_SECONDS` | `300` | Delay between previous-day scheduler passes |
| `LOG_LEVEL` | `INFO` | Reserved runtime logging setting |

Local CLI commands do not auto-load `.env`:

```bash
set -a
source .env
set +a
rpc-state-indexer status
```

Compose services load `${ENV_FILE:-.env}` directly.

## 9. Safe catalog expansion checklist

For each new token or pool:

1. Gather deployment block, date window, bytecode/proxy evidence, decimals, and event
   semantics from primary chain data.
2. Add or update the smallest committed ABI fragment needed for discovery.
3. Add the target row and an explicit job selector or class selection.
4. Run `rpc-state-indexer validate-config`.
5. Add an offline test for unusual event or call semantics.
6. Probe the oldest intended historical block using the configured archive provider.
7. Run a scoped one-date census first.
8. Inspect attempts, batches, errors, universe size, and result digests.
9. Run a full census only after the scoped read path is understood.
10. Start month-end history, then densify discrepant or required periods.

Do not make an unusual token pass by weakening strict decoding or converting an error
to zero. Model its behavior explicitly or leave it disabled.
