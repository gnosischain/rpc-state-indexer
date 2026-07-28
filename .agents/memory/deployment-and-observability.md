---
name: deployment-and-observability
type: project
updated: 2026-07-22
---

Production wrapping for rpc-state-indexer (added 2026-07). Spans two repos.

**CI (this repo):** `.github/workflows/build-and-release.yml`, mirrors cow-indexer — `push: main`,
job `test` (`make check` = ruff + guard scripts + pytest + **mypy on src AND tests**, then
`make validate-config`) → job `build` (GHCR via `GITHUB_TOKEN`, `docker buildx` **multi-arch
linux/amd64,linux/arm64** with `type=gha` cache, tags `:latest` + `:<short-sha>`). Image:
`ghcr.io/gnosischain/gc-rpc-state-indexer`. Note: `make check` runs mypy on tests too — keep test
files fully typed (strict), e.g. construct settings via the **alias** kwarg `RuntimeSettings(DAEMON_JOBS=…)`,
not the field name. requirements.txt has cross-platform hashes (120 for pydantic-core), so
`--require-hashes` works on both arches; the arm64 image builds natively on Apple Silicon.

**Deployment (infra repo):** `gnosis-analytics/infrastructure-gnosis-analytics-deployments/aws/
deployments/gnosis-analytics/scrapers/rpc-state-indexer/preview/*.tf` — Terraform→EKS, copied from
cow-indexer. Migration `kubernetes_job` (`args=["migrate"]`, gated `run_migration`) + daemon
`kubernetes_deployment` (`args=["daemon"]`, replicas=1, gated `enable_continuous`) + Service +
PodMonitor. Secrets via ESO from SSM ParameterStore: `analytics-preview-rpc-state-indexer`
(clickhouseHost/User/Password) + `-rpc` (rpcUrls, providerGroups). Probes `/live` + `/ready` :9090.
`terraform validate` passes (only `kubernetes_job`→`_v1` deprecation warnings, same as cow). DB is
`rpc_state_indexer` (wiped + re-migrated clean 000–010 for launch).

**Gotchas:**
- The app reads **`CLICKHOUSE_USER`** (cow reads `CLICKHOUSE_USERNAME`) and single comma-separated
  `RPC_URLS` + `RPC_PROVIDER_GROUPS` (cow uses per-chain). The ESO secret_key_ref maps accordingly.
- The **daemon runs every `cadence: daily` job each cycle** — the full catalog is thousands of
  targets (incl. CL tick sweeps). Scope with the `DAEMON_JOBS` setting (comma-separated job names;
  empty = all) and bound CL with `CL_MIN_ACTIVE_LIQUIDITY`.

**Deployment modularity (the two layers map differently):** Layer-1 ingestion "cases" are catalog
jobs; because of the **single-writer-per-chain** heartbeat they all run inside the ONE `daemon`
Deployment (5_continuous.tf) — they can't be separate concurrent pods. Per-deployment selection is
the `ingestion_jobs` list var → `DAEMON_JOBS`. Layer-2 compute is RPC-free + no writer guard, so it's
a **separate CronJob** (6_compute.tf, `enable_compute`) that shells `date -u -d yesterday` → `compute
--date` (no image change) and needs only the ClickHouse secret, not the RPC secret. Adding an
ingestion case = new collector+job in the app + append to `ingestion_jobs`; adding a compute module =
app REGISTRY only (the CronJob runs all). The README has the full step-by-step SSM/ESO secret setup.

**Backfill:** the daemon is **forward-only (yesterday each cycle)** — it never fills history or missed
days. `7_backfill.tf` is a one-shot `backfill --from --to [--daily|--month-end] [--job]` Job (gated
`run_backfill`) for history/gaps. It's a per-chain writer → **mutually exclusive with the daemon**
(pause `enable_continuous` first). Re-runs are safe (publication gate skips done target/date pairs).

**Metrics (this repo, `observability/metrics.py`):** added `census_publications_total{job,
target_kind,outcome}`, `daemon_cycles_total`, `daemon_job_failures_total{job}`,
`endpoint_healthy{provider_group,endpoint}`, `compute_rows_written_total{module}`; removed the dead
`coverage_gap_days`. The metrics/health server (`/live`,`/ready`,`/health`,`/metrics`) runs **only
in the daemon**. Verified live through the container (endpoint_healthy showed devops=0/internal=1).

**Dashboard + alerts (infra repo):** `dashboards/rpc-state-indexer-observability.json` (Grafana JSON,
uid `rpc-state-indexer-observability`, over Prometheus `$prometheus`=thanos-gnosisanalytics /
ClickHouse `$clickhouse`=af7kan6kjrq4gd (type `grafana-clickhouse-datasource`) / Loki
`$loki`=loki-gnosisanalytics) — hand-imported to `monitoring.gnosis.io`, NOT Terraform. Alerts:
`alerting/alerts/rpc-state-indexer.yaml` (Grafana-provisioned; `terraform apply` in `alerting/`).
The ClickHouse panel queries were validated against the live DB. Related: [[deploy-run-sequence]],
[[indexer-two-layer-architecture]].
