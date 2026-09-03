"""Writer-guard lock classes: census writers exclude census writers, discovery
writers exclude discovery writers, and the two classes coexist."""

from typing import Any, cast

import pytest

from rpc_state_indexer.service import ServiceError, WriterGuard
from rpc_state_indexer.storage.repositories import ClickHouseRepository


class FakeRepository:
    """Returns scripted fresh-writer responses and records the queried scope."""

    def __init__(self, responses: list[tuple[str, ...]]) -> None:
        self.responses = responses
        self.scope_queries: list[bool] = []
        self.heartbeats: list[dict[str, Any]] = []

    def fresh_writer_processes(
        self,
        chain_id: int,
        stale_seconds: int,
        *,
        discovery_scope: bool = False,
    ) -> tuple[str, ...]:
        self.scope_queries.append(discovery_scope)
        return self.responses.pop(0)

    def heartbeat(self, row: dict[str, Any]) -> None:
        self.heartbeats.append(row)


def _guard(repository: FakeRepository, operation: str) -> WriterGuard:
    return WriterGuard(
        cast(ClickHouseRepository, repository),
        chain_id=100,
        operation=operation,
        stale_seconds=120,
    )


def test_scope_derives_from_operation() -> None:
    repository = FakeRepository([])
    assert _guard(repository, "discover").discovery_scope is True
    for operation in ("daemon", "census", "backfill", "densify", "sweep", "bench"):
        assert _guard(repository, operation).discovery_scope is False


@pytest.mark.asyncio
async def test_census_guard_refuses_fresh_census_writer() -> None:
    repository = FakeRepository([("other-process",)])
    guard = _guard(repository, "daemon")

    with pytest.raises(ServiceError, match="another writer heartbeat is fresh"):
        await guard.acquire()

    assert repository.scope_queries == [False]
    assert repository.heartbeats == []


@pytest.mark.asyncio
async def test_discovery_guard_refuses_fresh_discovery_writer() -> None:
    repository = FakeRepository([("other-discovery-process",)])
    guard = _guard(repository, "discover")

    with pytest.raises(ServiceError, match="another writer heartbeat is fresh"):
        await guard.acquire()

    assert repository.scope_queries == [True]


@pytest.mark.asyncio
async def test_discovery_guard_acquires_while_census_daemon_holds_its_lock() -> None:
    # The repository only ever reports writers of the queried scope; a fresh
    # census daemon is invisible to a discovery-scope query, so acquisition
    # sees an empty class before the write and only itself after it.
    repository = FakeRepository([()])
    guard = _guard(repository, "discover")
    repository.responses.append((str(guard.process_id),))

    await guard.acquire()
    try:
        assert repository.scope_queries == [True, True]
        assert [row["operation"] for row in repository.heartbeats] == ["discover"]
        assert guard.healthy is True
    finally:
        await guard.release()

    assert repository.heartbeats[-1]["details_json"] == '{"state": "released"}'


@pytest.mark.asyncio
async def test_race_check_only_considers_same_scope_writers() -> None:
    repository = FakeRepository([()])
    guard = _guard(repository, "discover")
    repository.responses.append((str(guard.process_id), "racing-discovery-writer"))

    with pytest.raises(ServiceError, match="writer race detected"):
        await guard.acquire()

    assert repository.scope_queries == [True, True]
    # The losing writer releases its own beat so the winner is not blocked.
    assert repository.heartbeats[-1]["details_json"] == '{"state": "released"}'
