"""Which inserts may be asynchronous, and which must never be."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from rpc_state_indexer.storage.repositories import ClickHouseRepository


class CapturingClient:
    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []

    def insert(self, table: str, data: Any, *, column_names: Any, settings: dict[str, Any]) -> None:
        self.inserts.append({"table": table, "settings": settings})


def _repo() -> tuple[ClickHouseRepository, CapturingClient]:
    client = CapturingClient()
    return ClickHouseRepository(client, "db"), client


def test_attempt_state_is_async_and_does_not_wait() -> None:
    repository, client = _repo()
    repository.insert_attempt_state({"chain_id": 100, "attempt_id": uuid4(), "status": "started"})
    assert client.inserts[0]["settings"] == {"async_insert": 1, "wait_for_async_insert": 0}


def test_gating_and_read_back_tables_stay_synchronous() -> None:
    repository, client = _repo()
    attempt = uuid4()
    repository.insert_token_balances(
        [{"chain_id": 100, "attempt_id": attempt, "holder_address": "0xabc", "balance_raw": 1}],
        attempt_id=attempt,
        batch_sequence=0,
    )
    repository.append_publication(
        {"chain_id": 100, "attempt_id": attempt, "publication_id": uuid4()}
    )
    repository.heartbeat(
        {"chain_id": 100, "process_id": uuid4(), "operation": "census", "hostname": "h",
         "details_json": "{}", "started_at": datetime.now(UTC), "heartbeat_at": datetime.now(UTC)}
    )
    tables = [i["table"] for i in client.inserts]
    assert tables == ["db.token_balances", "db.census_publications", "db.writer_heartbeats"]
    for insert in client.inserts:
        assert insert["settings"]["async_insert"] == 0, insert["table"]
    # observations additionally carry their dedup token, unchanged
    assert client.inserts[0]["settings"]["insert_deduplication_token"] == f"{attempt}:balances:0"
