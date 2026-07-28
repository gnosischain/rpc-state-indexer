from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest

from rpc_state_indexer.config.models import SweepConfig
from rpc_state_indexer.core.discovery import (
    BlockRange,
    DiscoveryRangeFailed,
    DiscoveryResponseError,
    LogFilter,
    StrictLogScanner,
)
from rpc_state_indexer.core.sweep_service import (
    SweepService,
    scan_windows,
    wallet_topic,
)
from rpc_state_indexer.rpc.errors import RpcProviderLimit

WALLET_A = "0x00000000000000000000000000000000000000aa"
WALLET_B = "0x00000000000000000000000000000000000000bb"
CONTRACT = "0x1000000000000000000000000000000000000001"
TRANSFER = "0x" + "dd" * 32


class FakeEndpoint:
    fingerprint = "sweep-endpoint"


def hash32(number: int) -> str:
    return f"0x{number:064x}"


def sweep_log(
    block_number: int,
    *,
    wallet: str = WALLET_A,
    position: int = 2,
    log_index: int = 0,
    address: str = CONTRACT,
    topic0: str = TRANSFER,
    extra_topics: int = 0,
) -> dict[str, Any]:
    topics = [topic0, hash32(1), hash32(2), hash32(3)][: position + 1]
    topics[position] = wallet_topic(wallet)
    topics.extend(hash32(9) for _ in range(extra_topics))
    return {
        "address": address,
        "topics": topics,
        "data": "0x",
        "blockNumber": hex(block_number),
        "blockHash": hash32(10_000 + block_number),
        "transactionHash": hash32(20_000 + block_number),
        "transactionIndex": "0x0",
        "logIndex": hex(log_index),
        "removed": False,
    }


class FakeSweepRpc:
    """Serves logs by topic-position filter, mimicking address-less eth_getLogs."""

    def __init__(
        self,
        logs: list[dict[str, Any]],
        *,
        max_span: int | None = None,
        fail_ranges_containing: int | None = None,
    ) -> None:
        self.logs = logs
        self.max_span = max_span
        self.fail_ranges_containing = fail_ranges_containing
        self.requests: list[dict[str, Any]] = []

    async def call(
        self,
        method: str,
        params: Sequence[Any],
        *,
        historical_block: int | None = None,
        require_eip1898: bool = False,
    ) -> tuple[Any, FakeEndpoint]:
        del historical_block, require_eip1898
        assert method == "eth_getLogs"
        request = cast(dict[str, Any], params[0])
        self.requests.append(request)
        assert "address" not in request
        start = int(request["fromBlock"], 16)
        end_exclusive = int(request["toBlock"], 16) + 1
        if self.max_span is not None and end_exclusive - start > self.max_span:
            raise RpcProviderLimit("block range too wide")
        if (
            self.fail_ranges_containing is not None
            and start <= self.fail_ranges_containing < end_exclusive
        ):
            raise RpcProviderLimit("single block remains too large")
        topics = request["topics"]
        result = []
        for item in self.logs:
            block = int(item["blockNumber"], 16)
            if not start <= block < end_exclusive:
                continue
            matched = True
            for index, constraint in enumerate(topics):
                if constraint is None:
                    continue
                values = constraint if isinstance(constraint, list) else [constraint]
                if index >= len(item["topics"]) or item["topics"][index] not in values:
                    matched = False
                    break
            if matched:
                result.append(item)
        return result, FakeEndpoint()


class RecordingStore:
    def __init__(
        self, completed: dict[tuple[str, int], list[tuple[int, int]]] | None = None
    ) -> None:
        self.completed = completed or {}
        self.range_rows: list[dict[str, Any]] = []
        self.log_rows: list[dict[str, Any]] = []

    def completed_sweep_ranges(
        self, chain_id: int, wallet_address: str, topic_position: int
    ) -> list[tuple[int, int]]:
        del chain_id
        return self.completed.get((wallet_address, topic_position), [])

    def insert_sweep_ranges(self, rows: list[dict[str, object]]) -> int:
        self.range_rows.extend(cast(list[dict[str, Any]], rows))
        return len(rows)

    def insert_wallet_interaction_logs(self, rows: list[dict[str, object]]) -> int:
        self.log_rows.extend(cast(list[dict[str, Any]], rows))
        return len(rows)


def make_service(
    rpc: FakeSweepRpc,
    store: RecordingStore,
    *,
    chunk: int = 100,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> SweepService:
    return SweepService(
        chain_id=1,
        rpc=cast(Any, rpc),
        store=store,
        initial_chunk_size=chunk,
        provider_result_cap=100,
        on_progress=(
            None
            if events is None
            else lambda event, **fields: events.append((event, fields))
        ),
    )


def make_sweep(
    positions: list[int] | None = None,
    start_block: int = 0,
    checkpoint_blocks: int = 100_000,
) -> SweepConfig:
    return SweepConfig(
        name="treasury_interactions",
        universe="treasury",
        topic_positions=positions or [2],
        start_block=start_block,
        checkpoint_blocks=checkpoint_blocks,
    )


def test_log_filter_rejects_unconstrained_and_malformed() -> None:
    with pytest.raises(ValueError):
        LogFilter(address=None, topics=(None, None))
    with pytest.raises(ValueError):
        LogFilter(address=None, topics=(None, ()))
    with pytest.raises(ValueError):
        LogFilter(address=None, topics=((TRANSFER.upper(),),))
    with pytest.raises(ValueError):
        LogFilter(address="0xNOT", topics=((TRANSFER,),))
    with pytest.raises(ValueError):
        LogFilter(address=None, topics=(None, None, None, None, (TRANSFER,)))


def test_log_filter_params_trims_trailing_unconstrained_positions() -> None:
    log_filter = LogFilter(address=None, topics=((TRANSFER,), None))
    params = log_filter.params(BlockRange(0, 4))
    assert params["topics"] == [[TRANSFER]]
    assert "address" not in params


def test_sweep_config_rejects_bad_positions() -> None:
    with pytest.raises(ValueError):
        SweepConfig(
            name="treasury_interactions",
            universe="treasury",
            topic_positions=[],
            start_block=0,
        )
    with pytest.raises(ValueError):
        make_sweep(positions=[0])
    with pytest.raises(ValueError):
        make_sweep(positions=[4])
    with pytest.raises(ValueError):
        make_sweep(positions=[1, 1])


@pytest.mark.asyncio
async def test_scan_filter_rejects_log_outside_topic_filter() -> None:
    stray = sweep_log(2, wallet=WALLET_A)
    stray["topics"][2] = hash32(77)

    class LeakyRpc(FakeSweepRpc):
        async def call(
            self, method: str, params: Sequence[Any], **kwargs: Any
        ) -> tuple[Any, FakeEndpoint]:
            del kwargs
            self.requests.append(cast(dict[str, Any], params[0]))
            return [stray], FakeEndpoint()

    scanner = StrictLogScanner(
        cast(Any, LeakyRpc([])), initial_chunk_size=100, provider_result_cap=100
    )
    log_filter = LogFilter(
        address=None, topics=(None, None, (wallet_topic(WALLET_A),))
    )
    with pytest.raises(DiscoveryResponseError):
        await scanner.scan_filter(
            log_filter=log_filter, start_block=0, end_block_exclusive=8
        )


@pytest.mark.asyncio
async def test_sweep_attributes_logs_to_the_matching_wallet() -> None:
    logs = [
        sweep_log(1, wallet=WALLET_A),
        sweep_log(2, wallet=WALLET_B, extra_topics=1),
        sweep_log(3, wallet=WALLET_A, address="0x2000000000000000000000000000000000000002"),
    ]
    rpc = FakeSweepRpc(logs)
    store = RecordingStore()
    await make_service(rpc, store).advance(
        make_sweep(), (WALLET_A, WALLET_B), anchor_block=7, anchor_hash=hash32(7)
    )

    by_wallet = {
        (row["wallet_address"], row["block_number"]): row for row in store.log_rows
    }
    assert set(by_wallet) == {(WALLET_A, 1), (WALLET_B, 2), (WALLET_A, 3)}
    assert by_wallet[(WALLET_B, 2)]["topic_count"] == 4
    assert by_wallet[(WALLET_A, 3)]["contract_address"] == (
        "0x2000000000000000000000000000000000000002"
    )
    completed = [row for row in store.range_rows if row["status"] == "completed"]
    # One batched scan covering both wallets writes coverage per wallet.
    assert {row["wallet_address"] for row in completed} == {WALLET_A, WALLET_B}
    assert all(row["range_start_block"] == 0 for row in completed)
    assert all(row["range_end_block_exclusive"] == 8 for row in completed)
    a_rows = [row for row in completed if row["wallet_address"] == WALLET_A]
    assert sum(int(row["log_count"]) for row in a_rows) == 2


@pytest.mark.asyncio
async def test_sweep_only_backfills_the_wallet_with_a_gap() -> None:
    store = RecordingStore(
        completed={
            (WALLET_A, 2): [(0, 8)],
        }
    )
    rpc = FakeSweepRpc([sweep_log(5, wallet=WALLET_B)])
    await make_service(rpc, store).advance(
        make_sweep(), (WALLET_A, WALLET_B), anchor_block=7, anchor_hash=hash32(7)
    )

    assert len(rpc.requests) == 1
    only_constraint = rpc.requests[0]["topics"][2]
    assert only_constraint == [wallet_topic(WALLET_B)]
    assert {row["wallet_address"] for row in store.range_rows} == {WALLET_B}


@pytest.mark.asyncio
async def test_sweep_scans_each_configured_topic_position() -> None:
    rpc = FakeSweepRpc([])
    store = RecordingStore()
    await make_service(rpc, store).advance(
        make_sweep(positions=[1, 2, 3]),
        (WALLET_A,),
        anchor_block=3,
        anchor_hash=hash32(3),
    )

    constrained = [
        max(
            index
            for index, constraint in enumerate(request["topics"])
            if constraint is not None
        )
        for request in rpc.requests
    ]
    assert constrained == [1, 2, 3]
    assert {row["topic_position"] for row in store.range_rows} == {1, 2, 3}


@pytest.mark.asyncio
async def test_sweep_failure_records_failed_range_per_wallet_and_raises() -> None:
    rpc = FakeSweepRpc([], fail_ranges_containing=3)
    store = RecordingStore()
    with pytest.raises(DiscoveryRangeFailed):
        await make_service(rpc, store).advance(
            make_sweep(), (WALLET_A, WALLET_B), anchor_block=7, anchor_hash=hash32(7)
        )

    failed = [row for row in store.range_rows if row["status"] == "failed"]
    assert {row["wallet_address"] for row in failed} == {WALLET_A, WALLET_B}
    assert all(row["error_class"] == "DiscoveryRangeFailed" for row in failed)
    assert all("too large" in row["error_message"] for row in failed)
    # A failing block never lets coverage advance past it.
    completed = [row for row in store.range_rows if row["status"] == "completed"]
    assert all(row["range_end_block_exclusive"] <= 3 for row in completed)


@pytest.mark.asyncio
async def test_sweep_splits_on_provider_limits_and_proves_coverage() -> None:
    logs = [sweep_log(block, wallet=WALLET_A) for block in (0, 3, 6)]
    rpc = FakeSweepRpc(logs, max_span=2)
    store = RecordingStore()
    await make_service(rpc, store, chunk=8).advance(
        make_sweep(), (WALLET_A,), anchor_block=7, anchor_hash=hash32(7)
    )

    assert {row["block_number"] for row in store.log_rows} == {0, 3, 6}
    completed = sorted(
        (row["range_start_block"], row["range_end_block_exclusive"])
        for row in store.range_rows
        if row["status"] == "completed"
    )
    frontier = 0
    for start, end in completed:
        assert start <= frontier
        frontier = max(frontier, end)
    assert frontier == 8


def test_scan_windows_tiles_a_range_without_gaps_or_overlap() -> None:
    windows = list(scan_windows(BlockRange(0, 25), 10))
    assert [(w.start, w.end_exclusive) for w in windows] == [(0, 10), (10, 20), (20, 25)]
    with pytest.raises(ValueError):
        list(scan_windows(BlockRange(0, 10), 0))


@pytest.mark.asyncio
async def test_sweep_commits_each_window_before_starting_the_next() -> None:
    """A multi-year gap must persist incrementally, not accumulate to the end."""

    commits: list[tuple[int, int]] = []

    class CommitTrackingStore(RecordingStore):
        def insert_sweep_ranges(self, rows: list[dict[str, object]]) -> int:
            for row in rows:
                commits.append(
                    (
                        cast(int, row["range_start_block"]),
                        cast(int, row["range_end_block_exclusive"]),
                    )
                )
            return super().insert_sweep_ranges(rows)

    store = CommitTrackingStore()
    rpc = FakeSweepRpc([sweep_log(block, wallet=WALLET_A) for block in (5, 15, 25)])
    events: list[tuple[str, dict[str, Any]]] = []
    await make_service(rpc, store, events=events).advance(
        make_sweep(checkpoint_blocks=10),
        (WALLET_A,),
        anchor_block=29,
        anchor_hash=hash32(29),
    )

    # Three separate commits, each bounded by the checkpoint size.
    assert sorted(set(commits)) == [(0, 10), (10, 20), (20, 30)]
    indexed = [fields for name, fields in events if name == "sweep_window_indexed"]
    assert [item["window"] for item in indexed] == [1, 2, 3]
    assert all(item["windows"] == 3 for item in indexed)
    assert [item["logs"] for item in indexed] == [1, 1, 1]
    start = [fields for name, fields in events if name == "sweep_position_start"]
    assert start[0]["windows"] == 3
    assert start[0]["from_block"] == 0
    assert start[0]["to_block"] == 29


@pytest.mark.asyncio
async def test_sweep_resumes_from_committed_windows_after_interruption() -> None:
    """An interrupted sweep re-scans only what was never committed."""

    store = RecordingStore(completed={(WALLET_A, 2): [(0, 10), (10, 20)]})
    rpc = FakeSweepRpc([sweep_log(25, wallet=WALLET_A)])
    await make_service(rpc, store).advance(
        make_sweep(checkpoint_blocks=10),
        (WALLET_A,),
        anchor_block=29,
        anchor_hash=hash32(29),
    )

    scanned = [
        (int(request["fromBlock"], 16), int(request["toBlock"], 16) + 1)
        for request in rpc.requests
    ]
    assert all(start >= 20 for start, _ in scanned), scanned
    assert {row["range_start_block"] for row in store.range_rows} == {20}


@pytest.mark.asyncio
async def test_sweep_rejects_empty_wallet_universe() -> None:
    with pytest.raises(ValueError):
        await make_service(FakeSweepRpc([]), RecordingStore()).advance(
            make_sweep(), (), anchor_block=7, anchor_hash=hash32(7)
        )
