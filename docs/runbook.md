# Operations runbook

This runbook covers the implemented CLI and daemon. Commands are shown for a local
virtual environment first, with Docker equivalents where useful.

## 1. Prerequisites

Provide:

- Python 3.11-3.14 or Docker;
- ClickHouse reachable over its HTTP(S) interface;
- at least one archive-capable JSON-RPC endpoint for the selected chain;
- two genuinely independent provider groups if the endpoint pool cannot perform
  EIP-1898 hash-pinned calls and pre-Multicall history is required;
- enough RPC allowance for initial event discovery and the configured census size.

The process needs ClickHouse DDL permission for `migrate` and insert/select permission
for normal operation. It does not need access to a dbt database.

## 2. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
make install-dev
cp .env.example .env
```

Edit `.env`. For local commands, export it explicitly:

```bash
set -a
source .env
set +a
```

The settings loader does not read `.env` automatically. Compose reads it through the
service `env_file` declaration.

Validate everything that does not require network access:

```bash
rpc-state-indexer validate-config
make check-fast
```

Expected starter-catalog output:

```text
valid: chain=gnosis chain_id=100 tokens=3/3 pools=1/1 jobs=4
```

## 3. Bootstrap ClickHouse

Apply migrations before any storage-backed command:

```bash
rpc-state-indexer migrate
```

Docker equivalent:

```bash
docker compose --profile migrations run --rm migrations
```

Migration files must match `NNN_lowercase_name.sql`, begin with `000`, and are
recorded by their full filename and SHA-256 checksum. Rerunning skips identical files.
Changing an already applied migration causes a checksum error; add a new numbered
migration instead.

The container entrypoint does not wait for ClickHouse. Ensure the endpoint is
reachable before starting the migration or daemon service.

## 4. Probe RPC endpoints

Run the endpoint safety probe after migrations so the result can be persisted:

```bash
rpc-state-indexer probe --persist
```

Example output does not reveal endpoint URLs:

```text
rpc_1: ok batch=1 eip1898=1 finality=1
rpc_2: ok batch=1 eip1898=1 finality=1
```

The command exits nonzero if any configured endpoint fails. The daemon and write
commands also probe at startup, mark failing endpoints unavailable for that process,
and proceed only if at least one safe endpoint remains. If no surviving endpoint
supports EIP-1898, the surviving pool must contain enough distinct provider groups for
the configured legacy quorum.

The probe verifies Multicall bytecode at its deployment block and separately proves a
strict state read at the earliest enabled token deployment (WXDAI block `11,173,937`
in the starter catalog). Before a large pre-Multicall backfill, also benchmark the
oldest intended date on the actual paid endpoint tier as described in
[Pre-Multicall history](pre-multicall-history.md).

## 5. Benchmark a pinned date

```bash
rpc-state-indexer bench --date 2025-07-31
```

The benchmark resolves a finalized day-end anchor, verifies token bytecode, exercises
the executor at that anchor, and appends endpoint-pool evidence to `rpc_benchmarks`.
Use an old date to test legacy archive behavior and a recent finalized date to test
Multicall behavior.

Runtime limits remain environment settings:

```dotenv
MULTICALL_BATCH_SIZE=250
LEGACY_RPC_BATCH_SIZE=100
RPC_CONCURRENCY=8
RPC_REQUESTS_PER_SECOND=30
```

Start conservatively. Provider behavior can vary with account tier and concurrent
traffic even after a successful benchmark; the executors adaptively split classified
provider-limit failures.

## 6. First discovery

Run full-holder discovery separately when bootstrapping so progress and provider
limits can be observed before balance collection:

```bash
rpc-state-indexer discover \
  --through 2025-07-31 \
  --job daily_erc20_full
```

`--through` defaults to yesterday. Without `--job`, discovery considers every token
job whose universe recursively includes `full_holders`. Pool and explicit-only jobs
do not require discovery.

The first scan starts at each target's configured deployment block. Subsequent runs
read completed half-open ranges from ClickHouse and request only gaps. A failure at a
range or single block exits nonzero and does not advance coverage past that range.
Rerun the same command after correcting provider availability; completed ranges are
reused.

Inspect the frontier straight from the completed ranges (the per-token
deployment block is read from config, so there is no dedicated frontier view):

```sql
SELECT
    token_address,
    topic0,
    min(range_start_block) AS coverage_start_block,
    max(range_end_block_exclusive) AS coverage_end_block_exclusive
FROM rpc_indexer.discovery_ranges FINAL
WHERE chain_id = 100 AND status = 'completed'
GROUP BY token_address, topic0
ORDER BY token_address, topic0;
```

A gap between adjacent completed ranges means discovery has not yet closed that
window; rerun the discover command to fill it. The requested census still advances
discovery through its anchor before collection.

## 7. One census

```bash
rpc-state-indexer census \
  --date 2025-07-31 \
  --job daily_erc20_full
```

The `--date` value is required. Without `--job`, the command runs all configured jobs
and targets active on that date. It resolves one exact day anchor, runs any required
discovery, creates new attempts, and publishes each successful target independently.

Successful progress is emitted as structured JSON, for example:

```json
{"event":"anchor_resolved","snapshot_date":"2025-07-31","block_number":123,"block_hash":"0x..."}
{"event":"discovery_complete","token":"WXDAI","through_block":123}
{"event":"census_published","job":"daily_erc20_full","target":"WXDAI","snapshot_date":"2025-07-31","attempt_id":"..."}
```

If one target fails, the service continues the remaining selected targets and exits
nonzero at the end. Failure output reports target and exception class without dumping
endpoint URLs or provider bodies.

Run health checks afterward:

```bash
rpc-state-indexer status
rpc-state-indexer validate
```

`status --json` and `validate --json` emit one machine-readable object.

### Derived compute (Layer 2)

After the concentrated-liquidity primitives are published (job `daily_cl_liquidity`),
recompute the derived liquidity profile for a date:

```bash
rpc-state-indexer compute --date 2025-07-31            # all registered modules
rpc-state-indexer compute --date 2025-07-31 --module cl_profile
```

`compute` reads only the `v_*_published` primitives and writes derived tables
(`pool_liquidity_profile`) — it is **RPC-free** (no anchor, no writer heartbeat) and
idempotent: re-running a date reproduces identical data rows. Each derived row carries
`source_attempt_id` + `source_result_digest` back to the verified snapshot it came from. To
sanity-check the reconstruction, the segment containing a pool's `current_tick` must have
`active_liquidity == liquidity()`:

```sql
SELECT s.pool_address, s.current_tick, s.liquidity, p.active_liquidity
FROM rpc_indexer.v_pool_cl_state_published AS s
INNER JOIN rpc_indexer.v_pool_liquidity_profile AS p
    ON p.pool_address = s.pool_address AND p.snapshot_date = s.snapshot_date
   AND s.current_tick >= p.tick_lower AND s.current_tick < p.tick_upper
WHERE s.snapshot_date = toDate('2025-07-31');
```

## 8. Historical strategy

Month-end anchors are the default backfill mode:

```bash
rpc-state-indexer backfill \
  --from 2025-07-01 \
  --to 2026-06-30 \
  --month-end \
  --job daily_erc20_full
```

`--month-end` is the default even when omitted. For a final partial month, the final
`--to` date is included as that month's candidate. This mode limits initial state-call
volume and localizes disagreement by month.

To request every day directly:

```bash
rpc-state-indexer backfill \
  --from 2026-06-01 \
  --to 2026-06-30 \
  --daily \
  --job daily_erc20_full
```

Or densify a selected range after month-end review:

```bash
rpc-state-indexer densify \
  --from 2026-06-01 \
  --to 2026-06-30 \
  --job daily_erc20_full
```

`densify` is the daily backfill path. Both bounds are inclusive. Already published
keys are skipped; failed unpublished keys get fresh attempt IDs on rerun.

Because the first full-holder job must discover events from target deployment, old
month-end balance reads do not avoid the initial log sweep. Discovery is incremental
and reused across later snapshots.

## 9. Daemon

Run one process for one selected chain:

```bash
docker compose --profile daemon up --build daemon
```

Local equivalent:

```bash
rpc-state-indexer daemon
```

On every scheduler pass, the daemon:

1. selects yesterday in UTC;
2. runs each `cadence: daily` job;
3. skips targets already visible in `v_publications_current`;
4. sleeps for `DAEMON_POLL_SECONDS` and repeats.

This is a catch-up retry loop for yesterday, not a general cron parser. Historical
gaps must be run with `backfill` or `densify`.

### Single-writer guard

Write operations acquire a chain-scoped heartbeat in `writer_heartbeats`. Startup
refuses when another heartbeat is newer than `WRITER_STALE_SECONDS`. The process
refreshes its heartbeat at roughly one third of that window and writes a released
state on clean shutdown.

There is currently no force-overlap flag. If a process died, wait for the configured
stale window and verify that no writer is actually alive before retrying.

## 10. Health and metrics

The daemon starts a lightweight HTTP server on `METRICS_PORT` (default `9090`):

| Endpoint | Behavior |
|---|---|
| `/live` | HTTP 200 while the process/HTTP server is alive |
| `/ready` | HTTP 200 only after service startup; otherwise 503 |
| `/health` | Always HTTP 200 with `{"status":"ready"}` or `{"status":"degraded"}` |
| `/metrics` | Prometheus exposition |

The listener is operationally optional: if binding the port raises an OS error, the
daemon continues without it. Treat absence of the endpoint as a monitoring failure.

Current bounded-cardinality metrics include:

- `rpc_indexer_census_calls_total{job,token}`
- `rpc_indexer_census_call_failures_total{reason}`
- `rpc_indexer_suspect_zeros_total{token}`
- `rpc_indexer_supply_residual_ppm{token}`
- `rpc_indexer_batch_sentinel_failures_total{reason}`
- `rpc_indexer_publish_lag_days{job,token}`
- `rpc_indexer_coverage_gap_days{job,token}`
- `rpc_indexer_rpc_batch_seconds{executor}`

Holder and pool addresses are not metric labels. The suspect-zero counter is expected
to remain exactly zero; errors are represented as errors, not zero observations.

## 11. Read the published contract

Never build a consumer on raw `token_balances` alone. Use the publication-gated views:

```sql
SELECT
    snapshot_date,
    token_address,
    holder_address,
    balance_raw,
    value_kind,
    anchor_block,
    anchor_hash,
    universe_hash,
    attempt_id
FROM rpc_indexer.v_token_balances_published
WHERE chain_id = 100
  AND job_name = 'daily_erc20_full'
  AND snapshot_date = toDate('2025-07-31');
```

Scalars:

```sql
SELECT
    snapshot_date,
    token_address,
    scalar_name,
    scalar_raw,
    anchor_block,
    attempt_id
FROM rpc_indexer.v_token_scalars_published
WHERE chain_id = 100
  AND snapshot_date = toDate('2025-07-31')
ORDER BY token_address, scalar_name;
```

Pools:

```sql
SELECT
    snapshot_date,
    pool_address,
    token_address,
    balance_raw,
    anchor_block,
    attempt_id
FROM rpc_indexer.v_pool_token_balances_published
WHERE chain_id = 100
  AND snapshot_date = toDate('2025-07-31');
```

All values are raw `UInt256`; join token decimals from the catalog or your own
metadata layer for display only.

## 12. Diagnose a failed target

List current attempt states:

```sql
SELECT
    job_name,
    target_kind,
    target_address,
    snapshot_date,
    attempt_id,
    status,
    executor_kind,
    block_reference_kind,
    universe_size,
    batches_total,
    batches_verified,
    observations_ok,
    observations_failed,
    error_class,
    error_message
FROM rpc_indexer.v_census_attempts_current
WHERE chain_id = 100
  AND snapshot_date = toDate('2025-07-31')
ORDER BY job_name, target_address, started_at;
```

Inspect terminal call failures:

```sql
SELECT
    job_name,
    target_address,
    attempt_id,
    subject_address,
    call_kind,
    batch_sequence,
    error_class,
    rpc_code,
    error_message
FROM rpc_indexer.v_census_errors_current
WHERE chain_id = 100
  AND snapshot_date = toDate('2025-07-31')
ORDER BY attempt_id, batch_sequence, subject_address;
```

Inspect batch evidence. Per-batch verification evidence is folded onto the attempt
row as the `batches_json` array (there is no separate batch table); unpack it with
`ARRAY JOIN`:

```sql
SELECT
    attempt_id,
    anchor_block,
    JSONExtractInt(batch, 'batch_sequence')          AS batch_sequence,
    JSONExtractString(batch, 'executor_kind')        AS executor_kind,
    JSONExtractString(batch, 'block_reference_kind') AS block_reference_kind,
    JSONExtractString(batch, 'anchor_hash')          AS anchor_hash,
    JSONExtractRaw(batch, 'provider_groups')         AS provider_groups,
    JSONExtractUInt(batch, 'body_call_count')        AS body_call_count,
    JSONExtractString(batch, 'result_digest')        AS result_digest,
    JSONExtractInt(batch, 'verified')                AS verified
FROM rpc_indexer.v_census_attempts_current
ARRAY JOIN JSONExtractArrayRaw(batches_json) AS batch
WHERE chain_id = 100
  AND snapshot_date = toDate('2025-07-31')
  AND status = 'verified'
ORDER BY attempt_id, batch_sequence;
```

Common interpretations:

| Symptom | Meaning/action |
|---|---|
| `terminal_errors_present` | One or more calls failed or decoded incorrectly; inspect `census_errors`, fix provider/catalog semantics, rerun |
| `holder_sum_equals_total_supply` failed | Full event-discovered universe does not explain exact supply; verify missing event types, deployment start, or token semantics |
| `scaled_holder_sum_equals_scaled_total_supply` failed | aToken scaled universe/supply mismatch; verify mapping and discovery events |
| `observation_readback_digest` failed | ClickHouse accepted rows do not reproduce the in-memory digest; stop and investigate persistence |
| `universe_readback_digest` failed | Frozen membership/provenance did not read back identically; stop and investigate persistence |
| `BatchVerificationError`/sentinel metric | Endpoint did not prove the pinned Multicall anchor; remove/fix endpoint |
| `ProviderQuorumMismatch` | Independent legacy providers disagree; do not publish, investigate archive/fork/proxy behavior |
| `DiscoveryRangeFailed` | A log range could not be proven complete; correct endpoint limitations and rerun |

There is no implemented `repair` command yet. Rerunning `census` for an unpublished
key creates a new attempt and leaves the failed attempt as diagnostic evidence. If a
valid publication already exists, normal census skips that key.

## 13. Conflicts and fail-closed validation

Run:

```bash
rpc-state-indexer validate
```

It exits nonzero when any of these counts is nonzero:

- anchor conflicts;
- publication conflicts;
- per-block transfer-log count conflicts;
- unfinished unpublished attempts;
- failed targets without a later publication for the same key;
- unresolved errors without a later publication for the same key.

Inspect conflicts directly:

```sql
SELECT *
FROM rpc_indexer.v_anchor_conflicts
WHERE chain_id = 100
ORDER BY snapshot_date;

SELECT *
FROM rpc_indexer.v_publication_conflicts
WHERE chain_id = 100
ORDER BY snapshot_date, job_name, target_address;
```

Do not delete a conflicting row as an automatic repair. Identify whether the catalog,
anchor, provider result, or persistence path changed. Publications with conflicting
signatures are already excluded from consumer views.

## 14. Config changes and historical reruns

At startup, the service registers an effective config hash for every enabled
job-target. Publication views require that current hash. If a material token, pool,
job, universe, chain, or vendored-file definition changes:

1. old raw attempts and publication rows remain as evidence;
2. old publications no longer join through `v_publications_eligible` for that target;
3. `publication_exists` returns false through the current view;
4. rerunning the date creates a new attempt under the new hash.

Treat a large config edit as a controlled reindex. Validate the catalog, run one date,
compare universe size and call plan, and only then backfill affected history.

### Incremental catalog refresh (dynamic pools)

New pools are onboarded without a full re-enumeration. `enumerate.py` keeps a committed
watermark (`scripts/catalog/watermark.json`, the last scanned head); `--incremental` scans
each source from `watermark - overlap` to head and decodes only new pool-creation events, then
advances the watermark. `assemble.py` is **additive**: existing tokens/pools/jobs win on
collision, new pools are appended, and no target or job (including `daily_cl_liquidity`) is
removed. `make refresh-catalog` runs the whole flow — enumerate inside the jobs container,
then assemble and `validate-config` on the host:

```bash
make refresh-catalog
```

This is safe because the effective-config hash is **per job-target** (not a global digest): a
new pool is a new target with its own hash, so existing pools' hashes and published history are
untouched — no reindex of existing data. Recommended operation: run it on a schedule in CI and
open a PR with the `config/` diff (preserving review), then rebuild the image (or live-mount
`config/` via the dev overlay) so the running service picks up the new targets. The Uniswap
`tick_spacing`/`fee` (and Algebra `tick_spacing=60`) are captured from the creation event, so
the CL collector skips a per-anchor read for freshly enumerated pools.

## 15. Test matrix

Offline checks, expected on every change:

```bash
make check-fast
make check
```

`check-fast` runs Ruff, the no-zero-default lint, the no-silent-RPC-failure lint, and
tests excluding integration, pinned-chain, and performance markers. `check` adds
strict mypy over source and tests.

Pinned-chain archive execution:

```bash
GNOSIS_ARCHIVE_RPC_URLS=https://rpc-a,https://rpc-b \
GNOSIS_ARCHIVE_PROVIDER_GROUPS=provider_a,provider_b \
python -m pytest -m pinned_chain tests/pinned_chain
```

ClickHouse migration integration:

```bash
CLICKHOUSE_TEST_HOST=localhost \
CLICKHOUSE_TEST_PORT=8123 \
CLICKHOUSE_TEST_USER=default \
CLICKHOUSE_TEST_PASSWORD='' \
CLICKHOUSE_TEST_SECURE=false \
CLICKHOUSE_TEST_VERIFY=false \
python -m pytest -m integration tests/integration/clickhouse
```

The integration test creates a random `rpc_indexer_test_<uuid>` database, applies all
migrations twice, checks idempotence, and drops that test database in cleanup.

## 16. Docker command reference

```bash
# Migrate
docker compose --profile migrations run --rm migrations

# One-shot CLI command in the jobs image
docker compose --profile jobs run --rm jobs \
  census --date 2025-07-31 --job daily_erc20_full

# Read-only state
docker compose --profile jobs run --rm jobs status
docker compose --profile jobs run --rm jobs validate

# Continuous service
docker compose --profile daemon up --build daemon
```

For local CLI execution, the Makefile exposes:

```text
validate-config, status, validate, discover, census, backfill, densify, bench
```

`make job ARGS='...'` is the generic Compose one-shot wrapper.

## 17. Production-readiness checklist

Before treating the service as a production source:

- [ ] Replace the starter catalog with the reviewed production inventory.
- [ ] Verify every deployment block, lifetime, event set, pool asset, and aToken index
      source from chain data.
- [ ] Run offline, pinned-chain, and ClickHouse integration checks.
- [ ] Verify the oldest required archive block on the actual paid endpoint tier.
- [ ] Benchmark both legacy and Multicall regimes when both are in scope.
- [ ] Complete an initial strict discovery and inspect frontiers/islands.
- [ ] Publish one date for every integrity mode and inspect attempt/read-back evidence.
- [ ] Start with month-end historical anchors; densify intentionally.
- [ ] Alert separately on RPC/indexer health, anchor conflicts, publication conflicts,
      and downstream data disagreement.
- [ ] Back up ClickHouse and protect migrations/config/ABI files as versioned inputs.
- [ ] Build downstream reconciliation only against `v_*_published` views.

The indexer can operate without downstream reconciliation, but reconciliation is what
turns its independent observations into an audit of another balance pipeline.
