from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rpc_state_indexer import service as service_module
from rpc_state_indexer.config.loader import load_catalog
from rpc_state_indexer.domain import BlockRef, ExecutorKind
from rpc_state_indexer.execution.base import (
    ContractCall,
    RawCallResult,
    VerificationEvidence,
    VerifiedBatchResult,
    digest_raw_results,
)
from rpc_state_indexer.runtime import earliest_archive_probe_token
from rpc_state_indexer.service import (
    IndexerService,
    ServiceError,
    _month_end_dates,
    benchmark_single_batch_ceiling,
    run_daemon,
)
from rpc_state_indexer.settings import RuntimeSettings

ANCHOR = BlockRef(100, "0x" + "11" * 32, "0x" + "22" * 32, 1234)
ROOT = Path(__file__).parents[2]


def test_month_end_backfill_includes_partial_last_month_anchor() -> None:
    assert _month_end_dates(date(2025, 1, 15), date(2025, 3, 10)) == (
        date(2025, 1, 31),
        date(2025, 2, 28),
        date(2025, 3, 10),
    )


def test_archive_probe_ignores_disabled_tokens() -> None:
    catalog = load_catalog(ROOT / "config", "gnosis")
    oldest = min(catalog.tokens.values(), key=lambda token: token.deployment_block)
    disabled_oldest = oldest.model_copy(update={"enabled": False})
    changed = replace(
        catalog,
        tokens={**catalog.tokens, oldest.address: disabled_oldest},
    )

    selected = earliest_archive_probe_token(changed)

    assert selected.enabled is True
    assert selected.address != oldest.address


class AdaptivelySplittingExecutor:
    def __init__(self, capacity: int) -> None:
        self.batch_size = 2
        self.capacity = capacity
        self.attempted_sizes: list[int] = []

    @staticmethod
    def _batch(calls: Sequence[ContractCall]) -> VerifiedBatchResult:
        results = tuple(
            RawCallResult(call.key, True, (1).to_bytes(32, "big"))
            for call in calls
        )
        return VerifiedBatchResult(
            results,
            VerificationEvidence(
                executor_kind=ExecutorKind.MULTICALL3,
                block_reference_kind="eip1898",
                anchor_hash=ANCHOR.block_hash,
                provider_groups=("provider-a",),
                result_digest=digest_raw_results(results),
                verified=True,
            ),
        )

    async def execute(
        self,
        calls: Sequence[ContractCall],
        anchor: BlockRef,
    ) -> list[VerifiedBatchResult]:
        assert anchor == ANCHOR
        self.attempted_sizes.append(len(calls))
        if len(calls) <= self.capacity:
            return [self._batch(calls)]
        midpoint = len(calls) // 2
        return [self._batch(calls[:midpoint]), self._batch(calls[midpoint:])]


@pytest.mark.asyncio
async def test_benchmark_requires_one_physical_batch_and_restores_runtime_size() -> None:
    executor = AdaptivelySplittingExecutor(capacity=5)

    outcome = await benchmark_single_batch_ceiling(
        executor,
        token_address="0x" + "33" * 20,
        anchor=ANCHOR,
        max_size=10,
    )

    assert outcome.max_batch_size == 5
    assert outcome.recommended_batch_size == 4
    assert executor.batch_size == 2
    assert max(executor.attempted_sizes) > executor.batch_size


class FailingGuard:
    async def release(self) -> None:
        raise RuntimeError("heartbeat store is unavailable")


class ClosingRuntime:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class ClosingClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class StartupRepository:
    def __init__(self, client: ClosingClient) -> None:
        self.client = client

    def ping(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_service_close_releases_every_resource_when_guard_cleanup_fails() -> None:
    subject = IndexerService(RuntimeSettings(), "test")
    runtime = ClosingRuntime()
    client = ClosingClient()
    subject.catalog = cast(Any, object())
    subject.guard = cast(Any, FailingGuard())
    subject.runtime = cast(Any, runtime)
    subject.repository = cast(Any, SimpleNamespace(client=client))

    await subject.close()

    assert runtime.closed is True
    assert client.closed is True
    assert subject.catalog is None
    assert subject.guard is None
    assert subject.runtime is None
    assert subject.repository is None


@pytest.mark.asyncio
async def test_service_open_closes_clickhouse_if_rpc_runtime_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ClosingClient()
    repository = StartupRepository(client)
    monkeypatch.setattr(
        service_module,
        "build_repository",
        lambda _settings: cast(Any, repository),
    )
    monkeypatch.setattr(
        service_module,
        "build_rpc_runtime",
        lambda _settings, _catalog: (_ for _ in ()).throw(RuntimeError("RPC setup failed")),
    )

    subject = IndexerService(RuntimeSettings(), "test")
    with pytest.raises(RuntimeError, match="RPC setup failed"):
        await subject.open()

    assert client.closed is True
    assert subject.repository is None


class FakeHealth:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_daemon_closes_health_listener_when_service_startup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    health = FakeHealth()

    async def fail_open(_settings: RuntimeSettings, _operation: str) -> IndexerService:
        raise ServiceError("startup failed")

    monkeypatch.setattr(service_module, "start_health_server", lambda *args, **kwargs: health)
    monkeypatch.setattr(service_module, "_with_service", fail_open)

    with pytest.raises(ServiceError, match="startup failed"):
        await run_daemon(settings=RuntimeSettings())

    assert health.closed is True
