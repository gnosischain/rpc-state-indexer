"""census() runs full-holder discovery only for the targets it is about to census.

2026-09-25, backfill `--job daily_curated_balances --from 2026-09-08 --to 2026-09-24`: the
first pod published 1,077 target-days and failed one (WXDAI 2026-09-20, ClickHouse code
241). Its retry pod had one target left, yet re-ran discovery for all 65 tokens on all 17
dates, hit a code 241 in USDC's discovery on 2026-09-23 (a date with nothing left to
publish), failed that date and exited 1, so the Job started another identical pass. These
tests pin the fix: the skip gate runs first, discovery covers only the pending targets and
their aliases, and discovery still completes before any publication.
"""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from rpc_state_indexer import service as service_module
from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.service import IndexerService, JobRunError, ServiceError, run_backfill
from rpc_state_indexer.settings import RuntimeSettings

ANCHOR = BlockRef(100, "0x" + "11" * 32, "0x" + "22" * 32, 1234)
DAY = date(2026, 9, 23)


def _token(symbol: str, *aliases: str) -> Any:
    return SimpleNamespace(
        address="0x" + symbol.lower().ljust(40, "0")[:40],
        symbol=symbol,
        universe_aliases=tuple("0x" + a.lower().ljust(40, "0")[:40] for a in aliases),
    )


def _job(name: str, *, universe: str = "holders", discovered: bool = False) -> Any:
    return SimpleNamespace(
        name=name,
        target_kind="tokens",
        universe=universe,
        token_selector=SimpleNamespace(discovered=discovered),
    )


class Log:
    """One ordered trail across prefetch, discovery and census, to pin the sequence."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def of(self, kind: str) -> list[str]:
        return [value for k, value in self.events if k == kind]


class FakeRepository:
    def __init__(self, log: Log, published: dict[str, set[str]]) -> None:
        self.log = log
        self.published = published
        self.terminal_errors: list[dict[str, Any]] = []

    def published_target_addresses(
        self,
        *,
        chain_id: int,
        job_name: str,
        target_kind: str,
        snapshot_date: date,
        any_config_hash: bool = True,
    ) -> frozenset[str]:
        self.log.events.append(("prefetch", job_name))
        return frozenset(self.published.get(job_name, set()))

    def insert_terminal_errors(self, rows: list[dict[str, Any]]) -> int:
        self.terminal_errors.extend(rows)
        return len(rows)

    def close_all(self) -> None:
        return None


class FakeDiscovery:
    """DiscoveryService.advance stand-in; symbols in ``fail`` raise like a code 241 would."""

    def __init__(self, log: Log, fail: set[str] | None = None) -> None:
        self.log = log
        self.fail = fail or set()

    async def advance(self, token: Any, *, anchor_block: int, anchor_hash: str) -> None:
        self.log.events.append(("discover", token.symbol))
        if token.symbol in self.fail:
            raise RuntimeError(f"code 241 while reading discovery_ranges for {token.symbol}")


class FakeRunner:
    def __init__(self, log: Log) -> None:
        self.log = log

    async def run_token(self, job: Any, token: Any, snapshot_date: date, anchor: BlockRef) -> UUID:
        self.log.events.append(("census", token.symbol))
        await asyncio.sleep(0)
        return uuid4()

    async def run_pool(self, *args: Any, **kwargs: Any) -> UUID:  # pragma: no cover
        raise AssertionError("no pool jobs in these tests")


class FakeRuntime:
    async def close(self) -> None:
        return None


def _service(
    monkeypatch: pytest.MonkeyPatch,
    log: Log,
    targets: dict[str, list[Any]],
    *,
    jobs: list[Any],
    published: dict[str, set[str]] | None = None,
    discovery_fails: set[str] | None = None,
    extra_tokens: tuple[Any, ...] = (),
) -> IndexerService:
    subject = IndexerService(RuntimeSettings(), "census")
    everything = [token for tokens in targets.values() for token in tokens] + list(extra_tokens)
    subject.repository = cast(Any, FakeRepository(log, published or {}))
    subject.catalog = cast(
        Any,
        SimpleNamespace(
            chain=SimpleNamespace(chain_id=100),
            universes={
                "holders": SimpleNamespace(kind="full_holders", of=()),
                "probe": SimpleNamespace(kind="explicit_list", of=()),
            },
            tokens={token.address: token for token in everything},
            # Like Catalog.token_targets: a discovered selector has no static list.
            token_targets=lambda job: (
                () if job.token_selector.discovered else tuple(targets[job.name])
            ),
        ),
    )
    subject.runtime = cast(Any, FakeRuntime())
    discovery = FakeDiscovery(log, discovery_fails)
    runner = FakeRunner(log)

    async def resolve_anchor(_day: date) -> BlockRef:
        return ANCHOR

    async def metadata_once() -> None:
        return None

    monkeypatch.setattr(subject, "resolve_anchor", resolve_anchor)
    monkeypatch.setattr(subject, "_resolve_metadata_once", metadata_once)
    monkeypatch.setattr(subject, "_runner", lambda: runner)
    monkeypatch.setattr(subject, "_discovery_service", lambda: discovery)
    monkeypatch.setattr(
        subject,
        "_jobs",
        lambda name: tuple(job for job in jobs if name is None or job.name == name),
    )
    monkeypatch.setattr(subject, "_token_targets", lambda job: tuple(targets[job.name]))
    monkeypatch.setattr(IndexerService, "_active", staticmethod(lambda *_a, **_k: True))
    return subject


@pytest.mark.asyncio
async def test_all_targets_published_runs_no_discovery_and_the_date_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Log()
    wxdai, usdc = _token("WXDAI"), _token("USDC")
    subject = _service(
        monkeypatch,
        log,
        {"daily_curated_balances": [wxdai, usdc]},
        jobs=[_job("daily_curated_balances")],
        published={"daily_curated_balances": {wxdai.address, usdc.address}},
        # Would fail the date exactly as on 2026-09-25 if discovery were still run.
        discovery_fails={"WXDAI", "USDC"},
    )

    attempts = await subject.census(DAY, job_name="daily_curated_balances")

    assert attempts == []
    assert log.of("discover") == []
    assert log.of("census") == []


@pytest.mark.asyncio
async def test_discovery_failure_of_a_published_target_cannot_fail_the_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Log()
    wxdai, usdc = _token("WXDAI"), _token("USDC")
    subject = _service(
        monkeypatch,
        log,
        {"daily_curated_balances": [wxdai, usdc]},
        jobs=[_job("daily_curated_balances")],
        published={"daily_curated_balances": {usdc.address}},
        discovery_fails={"USDC"},
    )

    attempts = await subject.census(DAY, job_name="daily_curated_balances")

    assert len(attempts) == 1
    assert log.of("discover") == ["WXDAI"]
    assert log.of("census") == ["WXDAI"]


@pytest.mark.asyncio
async def test_discovery_covers_only_pending_targets_and_their_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Log()
    # A is published (its alias Y must not be discovered); B is pending with alias X.
    a, b, c = _token("A", "Y"), _token("B", "X"), _token("C")
    x, y = _token("X"), _token("Y")
    subject = _service(
        monkeypatch,
        log,
        {"daily_curated_balances": [a, b, c]},
        jobs=[_job("daily_curated_balances")],
        published={"daily_curated_balances": {a.address}},
        extra_tokens=(x, y),
    )

    await subject.census(DAY, job_name="daily_curated_balances")

    # Address order, as discover() has always advanced them.
    assert log.of("discover") == ["B", "C", "X"]
    assert log.of("census") == ["B", "C"]


@pytest.mark.asyncio
async def test_pending_discovery_failure_still_blocks_every_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Log()
    subject = _service(
        monkeypatch,
        log,
        {"daily_curated_balances": [_token("A"), _token("B")]},
        jobs=[_job("daily_curated_balances")],
        discovery_fails={"B"},
    )

    with pytest.raises(JobRunError) as excinfo:
        await subject.census(DAY, job_name="daily_curated_balances")

    assert excinfo.value.failures == ["B: RuntimeError"]
    assert log.of("census") == []  # nothing publishes on an incomplete holder universe


@pytest.mark.asyncio
async def test_skip_gate_runs_for_every_job_before_discovery_and_discovery_before_census(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Log()
    done, pending = _token("DONE"), _token("TODO")
    subject = _service(
        monkeypatch,
        log,
        {"daily_atokens_full": [done], "daily_curated_balances": [pending]},
        jobs=[_job("daily_atokens_full"), _job("daily_curated_balances")],
        published={"daily_atokens_full": {done.address}},
    )

    await subject.census(DAY)

    assert log.events == [
        ("prefetch", "daily_atokens_full"),
        ("prefetch", "daily_curated_balances"),
        ("discover", "TODO"),
        ("census", "TODO"),
    ]


@pytest.mark.asyncio
async def test_jobs_outside_static_full_holder_discovery_are_never_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Log()
    subject = _service(
        monkeypatch,
        log,
        {"daily_token_supply": [_token("SCOPED")], "daily_treasury": [_token("SWEPT")]},
        jobs=[
            _job("daily_token_supply", universe="probe"),
            # A discovered selector was never in discovery (Catalog.token_targets is empty).
            _job("daily_treasury", discovered=True),
        ],
        discovery_fails={"SCOPED", "SWEPT"},
    )

    attempts = await subject.census(DAY)

    assert len(attempts) == 2
    assert log.of("discover") == []
    assert log.of("census") == ["SCOPED", "SWEPT"]


@pytest.mark.asyncio
async def test_discover_command_still_covers_every_active_full_holder_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `discover` (the discovery CronJob) has no skip gate: published tokens are advanced too.
    log = Log()
    a, b = _token("A", "X"), _token("B")
    subject = _service(
        monkeypatch,
        log,
        {"daily_curated_balances": [a, b], "daily_token_supply": [_token("SCOPED")]},
        jobs=[_job("daily_curated_balances"), _job("daily_token_supply", universe="probe")],
        published={"daily_curated_balances": {a.address, b.address}},
        extra_tokens=(_token("X"),),
    )

    anchor = await subject.discover(DAY)

    assert anchor == ANCHOR
    assert log.of("discover") == ["A", "B", "X"]
    assert log.of("prefetch") == []


class FakeHealth:
    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_backfill_retry_over_published_dates_exits_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The retry-pod shape: every date already published, discovery broken for one token.
    log = Log()
    wxdai, usdc = _token("WXDAI"), _token("USDC")
    subject = _service(
        monkeypatch,
        log,
        {"daily_curated_balances": [wxdai, usdc]},
        jobs=[_job("daily_curated_balances")],
        published={"daily_curated_balances": {wxdai.address, usdc.address}},
        discovery_fails={"USDC"},
    )

    async def open_subject(_settings: RuntimeSettings, _operation: str) -> Any:
        return subject

    monkeypatch.setattr(service_module, "start_health_server", lambda *a, **k: FakeHealth())
    monkeypatch.setattr(service_module, "_with_service", open_subject)

    await run_backfill(
        settings=RuntimeSettings(),
        from_date=date(2026, 9, 20),
        to_date=date(2026, 9, 24),
        job="daily_curated_balances",
        daily=True,
    )

    assert log.of("discover") == []
    assert log.kinds() == ["prefetch"] * 5


@pytest.mark.asyncio
async def test_backfill_still_fails_a_date_whose_pending_target_cannot_be_discovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = Log()
    usdc = _token("USDC")
    subject = _service(
        monkeypatch,
        log,
        {"daily_curated_balances": [usdc]},
        jobs=[_job("daily_curated_balances")],
        discovery_fails={"USDC"},
    )

    async def open_subject(_settings: RuntimeSettings, _operation: str) -> Any:
        return subject

    monkeypatch.setattr(service_module, "start_health_server", lambda *a, **k: FakeHealth())
    monkeypatch.setattr(service_module, "_with_service", open_subject)

    with pytest.raises(ServiceError, match="backfill finished with 1/1"):
        await run_backfill(
            settings=RuntimeSettings(),
            from_date=DAY,
            to_date=DAY,
            job="daily_curated_balances",
            daily=True,
        )
    assert log.of("census") == []
