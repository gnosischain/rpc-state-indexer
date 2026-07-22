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
`rpc_indexer_gnosis` (wiped + re-migrated clean 000–010 for launch).

**Gotchas:**
- The app reads **`CLICKHOUSE_USER`** (cow reads `CLICKHOUSE_USERNAME`) and single comma-separated
  `RPC_URLS` + `RPC_PROVIDER_GROUPS` (cow uses per-chain). The ESO secret_key_ref maps accordingly.
- The **daemon runs every `cadence: daily` job each cycle** — the full catalog is thousands of
  targets (incl. CL tick sweeps). Scope with the `DAEMON_JOBS` setting (comma-separated job names;
  empty = all) and bound CL with `CL_MIN_ACTIVE_LIQUIDITY`. tfvars defaults to
  `daily_pool_reserves,daily_cl_liquidity` + `poll 3600s`.

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
