"""The compute-before-census guard: a recent date is computed only once its source census
jobs have finished publishing; older dates are checked once; nothing computes a partial day."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from rpc_state_indexer import service
from rpc_state_indexer.compute import ClProfileModule


class ScriptedRepository:
    """Returns one scripted (started, published) reading per query, in order."""

    database = "rpc_state_indexer"

    def __init__(self, readings: list[tuple[int, int]]) -> None:
        self._readings = list(readings)
        self.calls = 0

    def query_rows(
        self, sql: str, parameters: dict[str, object] | None = None
    ) -> list[dict[str, int]]:
        assert "census_attempts FINAL" in sql and "census_publications" in sql
        assert parameters is not None and parameters["job_name"] == "daily_cl_liquidity"
        self.calls += 1
        started, published = self._readings.pop(0)
        return [{"started": started, "published": published}]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def _await(repo: ScriptedRepository, clock: FakeClock, **overrides: object) -> None:
    kwargs: dict[str, object] = {
        "chain_id": 100,
        "job_names": ("daily_cl_liquidity",),
        "snapshot_date": date(2026, 9, 23),
        "module_name": "cl_profile",
        "wait_seconds": 5400,
        "poll_seconds": 60,
        "sleep": clock.sleep,
        "clock": clock.monotonic,
        "today": date(2026, 9, 24),
    }
    kwargs.update(overrides)
    service._await_census_sources(repo, **kwargs)  # type: ignore[arg-type]


def test_ready_needs_publications_and_no_started_attempts() -> None:
    assert service.census_sources_ready({"j": (0, 2519)})
    assert not service.census_sources_ready({"j": (3, 2000)})
    assert not service.census_sources_ready({"j": (0, 0)})
    assert not service.census_sources_ready({})
    assert not service.census_sources_ready({"a": (0, 10), "b": (1, 10)})


def test_recent_date_waits_until_two_stable_ready_readings() -> None:
    repo = ScriptedRepository([(16, 1200), (0, 2519), (0, 2519)])
    clock = FakeClock()

    _await(repo, clock)

    assert repo.calls == 3
    assert clock.sleeps == [60, 60]


def test_recent_date_keeps_waiting_while_publications_still_grow() -> None:
    repo = ScriptedRepository([(0, 2400), (0, 2519), (0, 2519)])
    clock = FakeClock()

    _await(repo, clock)

    assert repo.calls == 3


def test_recent_date_fails_closed_at_the_deadline() -> None:
    repo = ScriptedRepository([(16, 100)] * 4)
    clock = FakeClock()

    with pytest.raises(service.ServiceError, match="source census not complete"):
        _await(repo, clock, wait_seconds=150)

    assert clock.sleeps == [60, 60, 30]


def test_old_date_is_checked_once_and_never_waits() -> None:
    repo = ScriptedRepository([(0, 2519)])
    clock = FakeClock()

    _await(repo, clock, snapshot_date=date(2026, 9, 1))

    assert repo.calls == 1 and clock.sleeps == []

    repo = ScriptedRepository([(0, 0)])
    with pytest.raises(service.ServiceError, match="source census not complete"):
        _await(repo, clock, snapshot_date=date(2026, 9, 1))
    assert clock.sleeps == []


def test_wait_zero_checks_once() -> None:
    clock = FakeClock()
    _await(ScriptedRepository([(0, 2519)]), clock, wait_seconds=0)
    assert clock.sleeps == []
    with pytest.raises(service.ServiceError):
        _await(ScriptedRepository([(2, 2519)]), clock, wait_seconds=0)


def test_source_jobs_come_from_the_catalog_by_integrity_mode() -> None:
    jobs = {
        "daily_cl_liquidity": SimpleNamespace(
            name="daily_cl_liquidity", integrity_mode="cl_liquidity", cadence="daily"
        ),
        "daily_treasury": SimpleNamespace(
            name="daily_treasury", integrity_mode="scoped", cadence="daily"
        ),
        "weekly_cl": SimpleNamespace(
            name="weekly_cl", integrity_mode="cl_liquidity", cadence="weekly"
        ),
    }
    catalog = SimpleNamespace(jobs=jobs)

    assert service._compute_source_jobs(catalog, ClProfileModule()) == ("daily_cl_liquidity",)  # type: ignore[arg-type]
    assert service._compute_source_jobs(catalog, SimpleNamespace(name="other")) == ()  # type: ignore[arg-type]
