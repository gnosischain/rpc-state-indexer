"""census() runs targets concurrently, bounded by CENSUS_TARGET_CONCURRENCY.

The serial loop was latency-bound at <1 target/s: ~13 sequential network
round-trips per target, one target at a time. These tests pin the semantics the
rewrite must keep — order, per-target failure isolation, the JobRunError at the
end — and prove targets actually overlap.
"""

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.service import IndexerService, JobRunError
from rpc_state_indexer.settings import RuntimeSettings

ANCHOR = BlockRef(100, "0x" + "11" * 32, "0x" + "22" * 32, 1234)
DAY = date(2026, 8, 31)


class FakeRepository:
    def __init__(self, published: frozenset[str] = frozenset()) -> None:
        self.published = published
        self.prefetches: list[tuple[str, str, date]] = []
        self.terminal_errors: list[dict[str, Any]] = []

    def published_target_addresses(
        self, *, chain_id: int, job_name: str, target_kind: str, snapshot_date: date
    ) -> frozenset[str]:
        self.prefetches.append((job_name, target_kind, snapshot_date))
        return self.published

    def insert_terminal_errors(self, rows: list[dict[str, Any]]) -> int:
        self.terminal_errors.extend(rows)
        return len(rows)


class FakeRunner:
    """Records overlap: how many run_token calls were in flight at once."""

    def __init__(self, *, fail: set[str] | None = None, delay: float = 0.03) -> None:
        self.fail = fail or set()
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self.started: list[str] = []

    async def run_token(self, job: Any, token: Any, snapshot_date: date, anchor: BlockRef) -> UUID:
        self.started.append(token.symbol)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            if token.symbol in self.fail:
                raise RuntimeError(f"boom {token.symbol}")
            return uuid4()
        finally:
            self.in_flight -= 1

    async def run_pool(self, *args: Any, **kwargs: Any) -> UUID:  # pragma: no cover
        raise AssertionError("no pool jobs in these tests")


def _token(symbol: str) -> Any:
    return SimpleNamespace(address="0x" + symbol.lower().ljust(40, "0")[:40], symbol=symbol)


def _service(
    monkeypatch: pytest.MonkeyPatch,
    runner: FakeRunner,
    repository: FakeRepository,
    tokens: list[Any],
    *,
    concurrency: int,
) -> IndexerService:
    settings = RuntimeSettings().model_copy(update={"census_target_concurrency": concurrency})
    subject = IndexerService(settings, "census")
    subject.repository = cast(Any, repository)
    subject.catalog = cast(Any, SimpleNamespace(chain=SimpleNamespace(chain_id=100)))
    subject.runtime = cast(Any, object())
    job = SimpleNamespace(name="daily_token_supply", target_kind="tokens", universe="supply_probe")

    async def resolve_anchor(_day: date) -> BlockRef:
        return ANCHOR

    async def discover(*_a: Any, **_k: Any) -> BlockRef:
        return ANCHOR

    async def metadata_once() -> None:
        return None

    monkeypatch.setattr(subject, "resolve_anchor", resolve_anchor)
    monkeypatch.setattr(subject, "discover", discover)
    monkeypatch.setattr(subject, "_resolve_metadata_once", metadata_once)
    monkeypatch.setattr(subject, "_runner", lambda: runner)
    monkeypatch.setattr(subject, "_jobs", lambda _name: (job,))
    monkeypatch.setattr(subject, "_token_targets", lambda _job: tuple(tokens))
    monkeypatch.setattr(IndexerService, "_active", staticmethod(lambda *_a, **_k: True))
    return subject


@pytest.mark.asyncio
async def test_targets_overlap_up_to_the_configured_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = [_token(f"T{i}") for i in range(12)]
    runner = FakeRunner()
    subject = _service(monkeypatch, runner, FakeRepository(), tokens, concurrency=4)

    attempts = await subject.census(DAY, job_name="daily_token_supply")

    assert len(attempts) == 12
    assert runner.max_in_flight == 4, runner.max_in_flight  # bounded, and actually parallel


@pytest.mark.asyncio
async def test_serial_when_concurrency_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = [_token(f"T{i}") for i in range(5)]
    runner = FakeRunner()
    subject = _service(monkeypatch, runner, FakeRepository(), tokens, concurrency=1)

    await subject.census(DAY, job_name="daily_token_supply")

    assert runner.max_in_flight == 1


@pytest.mark.asyncio
async def test_one_failure_does_not_stop_the_others_and_is_reported_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = [_token(s) for s in ("A", "B", "C", "D", "E", "F")]
    runner = FakeRunner(fail={"C", "E"})
    repository = FakeRepository()
    subject = _service(monkeypatch, runner, repository, tokens, concurrency=3)

    with pytest.raises(JobRunError) as excinfo:
        await subject.census(DAY, job_name="daily_token_supply")

    # Every target ran; failures are isolated and listed in target order.
    assert sorted(runner.started) == sorted(t.symbol for t in tokens)
    assert excinfo.value.failures == [
        "daily_token_supply/C: RuntimeError",
        "daily_token_supply/E: RuntimeError",
    ]
    # ...and each failure was persisted with its class (the error-persistence path).
    assert sorted(r["target_address"] for r in repository.terminal_errors) == sorted(
        t.address for t in tokens if t.symbol in {"C", "E"}
    )
    assert {r["error_class"] for r in repository.terminal_errors} == {"RuntimeError"}


@pytest.mark.asyncio
async def test_published_targets_are_prefetched_once_and_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = [_token(s) for s in ("A", "B", "C")]
    repository = FakeRepository(published=frozenset({tokens[1].address.lower()}))
    runner = FakeRunner()
    subject = _service(monkeypatch, runner, repository, tokens, concurrency=8)

    attempts = await subject.census(DAY, job_name="daily_token_supply")

    assert repository.prefetches == [("daily_token_supply", "token", DAY)]  # one query per job
    assert runner.started == ["A", "C"]  # B never ran
    assert len(attempts) == 2
