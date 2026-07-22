"""Guard the Prometheus metric catalog and the daemon-job scoping helper."""

from __future__ import annotations

from prometheus_client import REGISTRY

from rpc_state_indexer.settings import RuntimeSettings


def _metric_names() -> set[str]:
    return {metric.name for metric in REGISTRY.collect()}


def test_new_metrics_are_registered() -> None:
    names = _metric_names()
    # prometheus strips the _total suffix from the collected family name.
    assert "rpc_indexer_census_publications" in names
    assert "rpc_indexer_daemon_cycles" in names
    assert "rpc_indexer_daemon_job_failures" in names
    assert "rpc_indexer_endpoint_healthy" in names
    assert "rpc_indexer_compute_rows_written" in names


def test_dead_coverage_gap_metric_removed() -> None:
    assert "rpc_indexer_coverage_gap_days" not in _metric_names()


def test_census_publications_labels() -> None:
    from rpc_state_indexer.observability.metrics import CENSUS_PUBLICATIONS

    CENSUS_PUBLICATIONS.labels("daily_x", "pool", "published").inc()
    value = REGISTRY.get_sample_value(
        "rpc_indexer_census_publications_total",
        {"job": "daily_x", "target_kind": "pool", "outcome": "published"},
    )
    assert value is not None and value >= 1.0


def test_daemon_job_names_parsing() -> None:
    assert RuntimeSettings(DAEMON_JOBS="").daemon_job_names() is None
    assert RuntimeSettings(DAEMON_JOBS="  ").daemon_job_names() is None
    assert RuntimeSettings(DAEMON_JOBS="a, b ,a").daemon_job_names() == frozenset({"a", "b"})
