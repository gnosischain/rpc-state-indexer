# AGENTS.md — rpc-state-indexer

Standalone archive-RPC state indexer. Runtime data flow is JSON-RPC → verified
historical observations → the `rpc_indexer` ClickHouse database. The service must
not import or query dbt models.

## Non-negotiable correctness rules

- Never turn an RPC, decode, code, anchor, or subcall failure into zero.
- A successful uint256 return is exactly 32 bytes. Empty, short, and long returns fail.
- Every state call is pinned to an immutable, finalized historical block.
- At/after Multicall3 deployment, every batch has block/timestamp/parent-hash sentinels
  at both head and tail.
- Before Multicall3 deployment, use EIP-1898 or a number-pinned hash sandwich with
  matching results from distinct provider groups.
- Discovery is gap-free. A permanently failing block stops the scan.
- Partial attempts are diagnostic only. Published views join through the append-only
  publication gate.
- Repairs create new attempt IDs. Never delete or mutate a published attempt.
- Addresses are lowercase `0x` strings; amounts are exact integers, never floats.

## Workflow

1. Update configuration/domain contracts before runtime code that consumes them.
2. Add or update offline tests for every failure branch.
3. Run `make check-fast` for normal changes.
4. Run `make check` before handoff when dependencies are available.
5. Never print RPC URLs, credentials, or holder addresses as metric labels.

## Agent knowledge base — read this before non-trivial work

Shared, git-committed knowledge lives in [`.agents/`](.agents/). It is the fast path to
context and the record of traps this codebase has already hit.

- **Before** editing: skim [`.agents/MEMORY.md`](.agents/MEMORY.md) (durable facts) and
  [`.agents/LESSONS.md`](.agents/LESSONS.md) (symptom-indexed traps). Search LESSONS by
  the symptom you are seeing.
- **After** learning something durable: add or update one entry using the templates in
  [`.agents/templates/`](.agents/templates/), then add its one-line pointer to the
  matching index. One fact/lesson per file. See [`.agents/README.md`](.agents/README.md)
  for the full convention.
- Subsystem-local rules live in nested `AGENTS.md`:
  [`src/rpc_state_indexer/execution/AGENTS.md`](src/rpc_state_indexer/execution/AGENTS.md),
  [`migrations/AGENTS.md`](migrations/AGENTS.md), [`config/AGENTS.md`](config/AGENTS.md).

## Repo map

Entry and orchestration (`src/rpc_state_indexer/`):

- `cli.py` — Typer CLI, the `rpc-state-indexer` console entry point; side-effect-free imports.
- `service.py` — orchestration (`run_discover/census/backfill/densify/bench/daemon`, `WriterGuard`).
- `runtime.py` — explicit dependency construction (no import-time network).
- `settings.py` — env-only `RuntimeSettings` (pydantic-settings). Does **not** read `.env`.
- `domain.py` — frozen dataclasses/enums; `errors.py` — error types.

Subpackages:

- `config/` — typed YAML catalog loader, models, offline validation, effective-config hashing.
- `core/` — `anchors` (UTC day-end resolution), `discovery`/`discovery_service` (gap-free logs),
  `census` (append-only attempts + publication gate), `universes`.
- `collectors/` — `erc20`, `atoken`, `pools` state collectors.
- `evm/` — `abi`, `events`, `calldata`, strict `decoding`.
- `execution/` — `router` (regime by block), `multicall3`, `legacy_rpc_batch`, `batch_planner`, `verification`, `code`.
- `rpc/` — async `client`, `endpoint`/`endpoint_pool`, `capabilities`, `classification`.
- `storage/` — ClickHouse `clickhouse`/`repositories`, checksum-verified `migrations`, read-back `digests`.
- `observability/` — Prometheus `metrics`, HTTP `health` server.

Data (not code): `config/` (YAML catalog + vendored CSV), `abis/` (committed ABI fragments),
`migrations/` (`000`–`007` SQL). Prose docs: [`docs/`](docs/)
(`architecture.md`, `configuration.md`, `runbook.md`, `pre-multicall-history.md`).

## How to run

Config split: **YAML catalog = what to index; env vars = how the process runs.** The app
does not auto-load `.env`; Docker Compose injects it via `env_file`, and local runs need
`set -a; source .env; set +a` first. See [`.env.example`](.env.example) and
[`docs/configuration.md`](docs/configuration.md).

```bash
# Offline validation (no network)
make validate-config
make check-fast

# Docker Compose (the deployment artifact) — profiles select the service
docker compose --profile migrations run --rm migrations           # apply schema
docker compose --profile jobs run --rm jobs probe --persist        # verify RPC
docker compose --profile jobs run --rm jobs census --date <YYYY-MM-DD> --job <job>
docker compose --profile jobs run --rm jobs status                 # / validate
docker compose --profile daemon up --build daemon                  # continuous
```

Full operational detail — bootstrap, benchmarking, discovery, backfill, health/metrics,
failure diagnosis — is in [`docs/runbook.md`](docs/runbook.md).
