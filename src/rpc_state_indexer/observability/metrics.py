"""Bounded-cardinality Prometheus metrics for indexer health."""

from prometheus_client import Counter, Gauge, Histogram

CENSUS_CALLS = Counter(
    "rpc_indexer_census_calls_total",
    "Contract state calls materialized by a census.",
    ("job", "token"),
)
CENSUS_CALL_FAILURES = Counter(
    "rpc_indexer_census_call_failures_total",
    "Terminal direct-state call failures.",
    ("reason",),
)
SUSPECT_ZEROS = Counter(
    "rpc_indexer_suspect_zeros_total",
    "Values that might have been fabricated as zero; must remain zero.",
    ("token",),
)
SUPPLY_RESIDUAL_PPM = Gauge(
    "rpc_indexer_supply_residual_ppm",
    "Absolute holder-sum residual in parts per million (observability only).",
    ("token",),
)
BATCH_SENTINEL_FAILURES = Counter(
    "rpc_indexer_batch_sentinel_failures_total",
    "Multicall anchor sentinel verification failures.",
    ("reason",),
)
PUBLISH_LAG_DAYS = Gauge(
    "rpc_indexer_publish_lag_days",
    "Days from UTC snapshot date to successful publication.",
    ("job", "token"),
)
RPC_BATCH_SECONDS = Histogram(
    "rpc_indexer_rpc_batch_seconds",
    "Verified historical batch latency.",
    ("executor",),
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
CENSUS_PUBLICATIONS = Counter(
    "rpc_indexer_census_publications_total",
    "Census target outcomes per job (published, failed, or skipped-already-published).",
    ("job", "target_kind", "outcome"),
)
DAEMON_CYCLES = Counter(
    "rpc_indexer_daemon_cycles_total",
    "Daemon scheduler cycles started.",
)
DAEMON_JOB_FAILURES = Counter(
    "rpc_indexer_daemon_job_failures_total",
    "Daemon job runs that ended with at least one target failure.",
    ("job",),
)
ENDPOINT_HEALTHY = Gauge(
    "rpc_indexer_endpoint_healthy",
    "1 if the endpoint passed its last capability probe, else 0.",
    ("provider_group", "endpoint"),
)
COMPUTE_ROWS = Counter(
    "rpc_indexer_compute_rows_written_total",
    "Derived rows written by a compute module.",
    ("module",),
)

__all__ = [
    "BATCH_SENTINEL_FAILURES",
    "CENSUS_CALLS",
    "CENSUS_CALL_FAILURES",
    "CENSUS_PUBLICATIONS",
    "COMPUTE_ROWS",
    "DAEMON_CYCLES",
    "DAEMON_JOB_FAILURES",
    "ENDPOINT_HEALTHY",
    "PUBLISH_LAG_DAYS",
    "RPC_BATCH_SECONDS",
    "SUPPLY_RESIDUAL_PPM",
    "SUSPECT_ZEROS",
]
