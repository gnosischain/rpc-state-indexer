"""Per-thread ClickHouse clients and the publication prefetch."""

import threading
from datetime import date
from typing import Any

from rpc_state_indexer.storage.repositories import ClickHouseRepository


class FakeClient:
    instances = 0

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        FakeClient.instances += 1
        self.id = FakeClient.instances
        self.closed = False
        self.rows = rows or []

    def query(self, _sql: str, parameters: dict[str, Any] | None = None) -> Any:
        rows = self.rows

        class Result:
            def named_results(self) -> list[dict[str, Any]]:
                return rows

        return Result()

    def close(self) -> None:
        self.closed = True


def test_owner_thread_keeps_the_original_client_and_workers_get_their_own() -> None:
    owner = FakeClient()
    made: list[FakeClient] = []

    def factory() -> FakeClient:
        client = FakeClient()
        made.append(client)
        return client

    repository = ClickHouseRepository(owner, "db", client_factory=factory)
    assert repository.client is owner

    seen: dict[str, list[int]] = {"t1": [], "t2": []}

    def worker(name: str) -> None:
        seen[name].append(repository.client.id)
        seen[name].append(repository.client.id)  # same thread -> same client

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("t1", "t2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(made) == 2
    assert seen["t1"][0] == seen["t1"][1] and seen["t2"][0] == seen["t2"][1]
    assert seen["t1"][0] != seen["t2"][0]
    assert owner.id not in (seen["t1"][0], seen["t2"][0])

    repository.close_all()
    assert owner.closed and all(c.closed for c in made)


def test_without_a_factory_every_thread_shares_the_one_client() -> None:
    owner = FakeClient()
    repository = ClickHouseRepository(owner, "db")
    ids: list[int] = []
    t = threading.Thread(target=lambda: ids.append(repository.client.id))
    t.start()
    t.join()
    assert ids == [owner.id]


def test_published_target_addresses_returns_a_lowercased_set() -> None:
    client = FakeClient(rows=[{"target_address": "0xabc"}, {"target_address": "0xdef"}])
    repository = ClickHouseRepository(client, "db")

    published = repository.published_target_addresses(
        chain_id=100, job_name="j", target_kind="token", snapshot_date=date(2026, 8, 31)
    )

    assert published == frozenset({"0xabc", "0xdef"})
