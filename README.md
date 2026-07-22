# rpc-state-indexer

![state](img/header-banner.png)

`rpc-state-indexer` is a standalone historical EVM state indexer. It discovers
addresses from contract events, reads contract state at exact UTC day-end blocks,
and exposes only verified, complete attempts through ClickHouse views.

The runtime path is deliberately independent:

```text
archive JSON-RPC -> verified historical calls -> rpc_indexer ClickHouse database
```

It does **not** import, invoke, or query dbt. dbt (or any other analytics system) can
be a downstream consumer of the published views, but it is not an input to this
service. This separation is what makes the indexed observations useful as an
independent cross-check.

## Implementation status

The repository contains an executable first slice, not only a design document:

- exact finalized UTC-day anchor resolution;
- gap-free `eth_getLogs` discovery with adaptive range splitting;
- ERC-20 `balanceOf` and `totalSupply` collection;
- Aave/Spark aToken scaled balances, scaled supply, normalized income, and exact
  half-up ray reconstruction;
- pool reserves observed as `token.balanceOf(pool)`;
- verified state execution both before and after Multicall3 deployment;
- append-only attempts, errors, publications, and publication-gated views;
- ClickHouse migrations, operational CLI, Docker image, health endpoints, metrics,
  offline tests, and opt-in pinned-chain/integration tests.

The committed Gnosis catalog is intentionally a starter catalog: three tokens
(WXDAI, WETH, and aGnoWXDAI), one pool, and four jobs. Expanding it to the complete
production token, aToken, and pool inventory is catalog work still to do; the runtime
does not currently generate that catalog from another repository.

Native xDAI is not indexed in this version.

## Pre-Multicall3 history is included

On Gnosis, the configured Multicall3 deployment block is `21,022,491`. That block is
an execution-routing boundary, **not** the start of indexable history:

```text
configured target deployment       Multicall3 deployment
             |                               |
             v                               v
-------------+-------------------------------+--------------------> blocks
             legacy verified RPC batching    Multicall3 aggregate3
```

For an anchor below `21,022,491`, the indexer uses direct historical `eth_call`s in
JSON-RPC batches. It pins calls by EIP-1898 block hash when available. If EIP-1898 is
unavailable, it requires matching result digests from distinct provider groups and
checks the anchor hash immediately before and after each provider's calls. At and
after the deployment block it uses Multicall3 `aggregate3`, with block number,
timestamp, and parent-hash sentinels at both the beginning and end of every batch.

Therefore a token can be indexed from its own configured deployment block even when
that predates Multicall3. The remaining prerequisite is real archive availability at
the requested block. See [Pre-Multicall history](docs/pre-multicall-history.md) for
the exact guarantees and provider requirements.

## Data flow

```text
YAML catalog + committed ABIs
            |
            v
exact UTC day anchor (last block with timestamp < next midnight)
            |
            +--> strict event discovery --> holder_universe
            |                                  |
            |                                  v
            +--------------------------> frozen job universe
                                               |
                                               v
                        verified historical state executor
                         /                         \
            pre-Multicall JSON-RPC         Multicall3 + sentinels
                         \                         /
                          v                       v
                  attempt rows + observations + errors
                                     |
                         integrity + read-back digests
                                     |
                                     v
                            publication gate
                                     |
                                     v
                         v_*_published views
```

The full architecture and failure semantics are in
[Architecture and correctness](docs/architecture.md).

## Repository structure

```text
rpc-state-indexer/
├── abis/                       committed ABI fragments; no explorer lookup at runtime
├── config/
│   ├── chains.yaml             chain, finality, Multicall3, legacy and discovery rules
│   └── gnosis/
│       ├── tokens.yaml         token contracts and discovery event sets
│       ├── pools.yaml          pools and their configured assets
│       ├── universes.yaml      full-holder, explicit, union and intersection selectors
│       ├── jobs.yaml           target selector x universe x integrity mode
│       └── vendored/           local CSV inputs hashed into effective configuration
├── migrations/                 immutable numbered ClickHouse migrations and views
├── scripts/                    container entrypoint and correctness lints
├── src/rpc_state_indexer/
│   ├── collectors/             ERC-20, aToken and pool state collectors
│   ├── config/                 typed YAML loading, validation and hashing
│   ├── core/                   anchors, discovery, universes, census and publication
│   ├── evm/                    ABI/event/calldata encoding and strict decoding
│   ├── execution/              legacy RPC batches, Multicall3 and routing
│   ├── observability/          Prometheus metrics and HTTP health server
│   ├── rpc/                    async transport, endpoint pool and safety probes
│   ├── storage/                ClickHouse client, migrations, digests and repositories
│   ├── cli.py                  operator commands
│   ├── service.py              command and daemon orchestration
│   └── settings.py             environment-only operational settings
└── tests/
    ├── unit/                   offline fail-closed behavior tests
    ├── pinned_chain/           opt-in archive-RPC checks on both execution regimes
    └── integration/clickhouse/ opt-in migration checks against a real ClickHouse
```

## Quick start

Requirements: Python 3.11-3.14 or Docker, an archive-capable Gnosis RPC endpoint,
and ClickHouse. Two independently operated provider groups are required only for the
legacy fallback when no endpoint supports EIP-1898.

### 1. Install and validate offline

```bash
git clone <repository-url> rpc-state-indexer
cd rpc-state-indexer
python3 -m venv .venv
source .venv/bin/activate
make install-dev
cp .env.example .env
make validate-config
make check-fast
```

`validate-config` is offline. It validates the typed catalog and verifies that all
configured event definitions exist in the committed ABI fragments.

### 2. Set runtime credentials

Edit `.env`, then export it when invoking the CLI locally:

```bash
set -a
source .env
set +a
```

The application intentionally does not auto-read `.env`. Docker Compose does load it
through `env_file`.

The minimum runtime values are:

```dotenv
RPC_URLS=https://archive-rpc-a.example,https://archive-rpc-b.example
RPC_PROVIDER_GROUPS=provider_a,provider_b
CLICKHOUSE_HOST=clickhouse.example
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=...
CLICKHOUSE_DATABASE=rpc_indexer
```

Never use two URLs backed by the same operator under different provider-group names;
the legacy quorum is intended to establish independent agreement.

### 3. Create the ClickHouse schema and probe RPC safety

```bash
rpc-state-indexer migrate
rpc-state-indexer probe --persist
rpc-state-indexer bench --date 2025-07-31
```

The probe checks chain ID, HTTP batching, the finality tag, the pinned Multicall3
runtime code hash, positive plus negative EIP-1898 behavior, and a strict state call
at the earliest enabled token deployment (WXDAI block `11,173,937` in the starter
catalog). Adding an older enabled token automatically moves this archive-depth probe
backward. A benchmark or narrow census at the oldest intended date is still a useful
paid-tier throughput check before committing to a large run.

### 4. Run one discovery and census

```bash
rpc-state-indexer discover --through 2025-07-31 --job daily_erc20_full
rpc-state-indexer census --date 2025-07-31 --job daily_erc20_full
rpc-state-indexer status
rpc-state-indexer validate
```

`census` resolves the anchor and performs required full-holder discovery itself, so a
separate `discover` command is optional. Running it separately is useful for observing
the expensive first-time scan before state collection.

### 5. Backfill and then run continuously

```bash
# Historical anchors: month end is the default.
rpc-state-indexer backfill \
  --from 2025-07-01 \
  --to 2026-06-30 \
  --month-end \
  --job daily_erc20_full

# Fill every day only where needed.
rpc-state-indexer densify \
  --from 2026-06-01 \
  --to 2026-06-30 \
  --job daily_erc20_full

# Start the previous-day scheduler and health/metrics endpoint.
docker compose --profile daemon up --build daemon
```

The daemon tries every configured daily job for yesterday on each poll. Already
published `(chain, job, target, date)` keys are skipped.

For the complete operator procedure, recovery behavior, tests, health endpoints, and
diagnostic SQL, see [Operations runbook](docs/runbook.md).

## Configuration model

YAML defines **what** to index. Environment variables define **how** this process
runs. A token job is:

```text
token selector x named address universe x cadence x integrity mode
```

For example:

```yaml
# config/gnosis/universes.yaml
universes:
  treasury:
    kind: explicit_list
    source: vendored/treasury_addresses.csv
    address_column: address

# config/gnosis/jobs.yaml
jobs:
  daily_treasury:
    target_kind: tokens
    token_selector: {all_enabled: true}
    universe: treasury
    cadence: daily
    integrity_mode: scoped
```

The implemented universe types are `full_holders`, `explicit_list`, `union`, and
`intersect`. There is currently no live warehouse-query or dbt-derived universe
selector. Detailed field contracts and complete token/aToken/pool examples are in
[Configuration guide](docs/configuration.md).

## Published ClickHouse contract

Downstream consumers should read these views, not raw attempt tables:

- `rpc_indexer.v_token_balances_published`
- `rpc_indexer.v_token_scalars_published`
- `rpc_indexer.v_pool_token_balances_published`
- `rpc_indexer.v_publications_current`
- `rpc_indexer.v_coverage_calendar`

The views expose an attempt only when its publication matches the current config hash
and canonical day anchor. Conflicting publication signatures are excluded from
`v_publications_current` and reported by `v_publication_conflicts`.

The value tables are dense over the frozen probed universe: an observed zero is
stored as `0`; a failed or malformed call is stored in `census_errors` and blocks
publication. Absence and zero therefore remain distinct.

## Development checks

```bash
make check-fast   # ruff, two safety lints, offline unit tests
make check        # check-fast plus strict mypy
```

Optional real-service checks:

```bash
GNOSIS_ARCHIVE_RPC_URLS=https://rpc-a,https://rpc-b \
GNOSIS_ARCHIVE_PROVIDER_GROUPS=provider_a,provider_b \
pytest -m pinned_chain tests/pinned_chain

CLICKHOUSE_TEST_HOST=localhost \
CLICKHOUSE_TEST_SECURE=false \
pytest -m integration tests/integration/clickhouse
```

The pinned-chain test executes `totalSupply()` at block `20,000,000` through the
legacy executor and at block `21,022,500` through Multicall3, proving both sides of
the deployment boundary.

## What is deliberately not here yet

- the full production catalog (the checked-in catalog is the executable starter);
- native xDAI state indexing;
- a `repair` CLI (a failed unpublished key can be rerun and gets a new attempt ID;
  the repair-request table exists but has no command handler yet);
- cross-chain aggregation or bridge identity;
- downstream reconciliation models and alerts;
- dbt or warehouse-derived address universes;
- transaction, trace, raw block, or intra-day transfer attribution indexing.

These limits do not weaken the central contract: all values that reach a published
view come solely from pinned, verified RPC observations, with any reconstruction
identified explicitly by `value_kind` and performed in exact integer arithmetic.
