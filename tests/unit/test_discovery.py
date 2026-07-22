from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest

from rpc_state_indexer.core.discovery import (
    BlockRange,
    DiscoveryRangeFailed,
    DiscoveryResponseError,
    StrictLogScanner,
    contiguous_frontier,
    coverage_gaps,
    extract_holders,
    merge_coverage,
)
from rpc_state_indexer.rpc.errors import RpcProviderLimit

TOKEN = "0x1000000000000000000000000000000000000001"
OTHER_TOKEN = "0x2000000000000000000000000000000000000002"
TOPIC0 = "0x" + "11" * 32
HOLDER = "0x3000000000000000000000000000000000000003"
ZERO_WORD = "0x" + "00" * 32
HOLDER_WORD = "0x" + "00" * 12 + HOLDER[2:]


class FakeEndpoint:
    fingerprint = "logs-endpoint"


def hash32(number: int) -> str:
    return f"0x{number:064x}"


def rpc_log(
    block_number: int,
    *,
    log_index: int = 0,
    address: str = TOKEN,
    topic0: str = TOPIC0,
    topics: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "address": address,
        "topics": topics or [topic0],
        "data": "0x",
        "blockNumber": hex(block_number),
        "blockHash": hash32(10_000 + block_number),
        "transactionHash": hash32(20_000 + block_number),
        "transactionIndex": "0x0",
        "logIndex": hex(log_index),
        "removed": False,
    }


class FakeLogsRpc:
    def __init__(
        self,
        logs: list[dict[str, Any]],
        *,
        max_span: int | None = None,
        permanently_failing_block: int | None = None,
        duplicate_results: bool = False,
    ) -> None:
        self.logs = logs
        self.max_span = max_span
        self.permanently_failing_block = permanently_failing_block
        self.duplicate_results = duplicate_results
        self.ranges: list[BlockRange] = []

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
        start = int(request["fromBlock"], 16)
        end_exclusive = int(request["toBlock"], 16) + 1
        block_range = BlockRange(start, end_exclusive)
        self.ranges.append(block_range)
        if self.max_span is not None and block_range.size > self.max_span:
            raise RpcProviderLimit("block range too wide")
        if (
            self.permanently_failing_block is not None
            and start <= self.permanently_failing_block < end_exclusive
        ):
            raise RpcProviderLimit("single block remains too large")
        result = [
            item
            for item in self.logs
            if start <= int(item["blockNumber"], 16) < end_exclusive
        ]
        if self.duplicate_results:
            result = result + result
        return result, FakeEndpoint()


@pytest.mark.asyncio
async def test_scanner_adaptively_splits_and_proves_full_coverage() -> None:
    rpc = FakeLogsRpc([rpc_log(1), rpc_log(4), rpc_log(7)], max_span=2)
    scanner = StrictLogScanner(
        cast(Any, rpc), initial_chunk_size=8, provider_result_cap=100
    )

    result = await scanner.scan(
        token_address=TOKEN,
        topic0=TOPIC0,
        start_block=0,
        end_block_exclusive=8,
    )

    assert [item.block_number for item in result.logs] == [1, 4, 7]
    assert result.frontier == 8
    assert coverage_gaps(
        result.requested_range,
        (item.block_range for item in result.completed_ranges),
    ) == ()
    assert BlockRange(0, 8) in rpc.ranges


@pytest.mark.asyncio
async def test_scanner_never_skips_a_permanently_failing_block() -> None:
    rpc = FakeLogsRpc([], max_span=2, permanently_failing_block=5)
    scanner = StrictLogScanner(
        cast(Any, rpc), initial_chunk_size=8, provider_result_cap=100
    )

    with pytest.raises(DiscoveryRangeFailed, match="cannot return complete logs for block 5"):
        await scanner.scan(
            token_address=TOKEN,
            topic0=TOPIC0,
            start_block=0,
            end_block_exclusive=8,
        )

    assert BlockRange(5, 6) in rpc.ranges


@pytest.mark.asyncio
async def test_equal_to_provider_cap_is_split_not_accepted() -> None:
    logs = [rpc_log(0), rpc_log(1)]
    rpc = FakeLogsRpc(logs)
    scanner = StrictLogScanner(
        cast(Any, rpc), initial_chunk_size=2, provider_result_cap=2
    )

    result = await scanner.scan(
        token_address=TOKEN,
        topic0=TOPIC0,
        start_block=0,
        end_block_exclusive=2,
    )

    assert [item.block_number for item in result.logs] == [0, 1]
    assert BlockRange(0, 1) in rpc.ranges
    assert BlockRange(1, 2) in rpc.ranges


@pytest.mark.asyncio
async def test_single_block_at_provider_cap_fails_closed() -> None:
    rpc = FakeLogsRpc([rpc_log(0), rpc_log(0, log_index=1)])
    scanner = StrictLogScanner(
        cast(Any, rpc), initial_chunk_size=1, provider_result_cap=2
    )

    with pytest.raises(DiscoveryRangeFailed, match="reached the provider result cap"):
        await scanner.scan(
            token_address=TOKEN,
            topic0=TOPIC0,
            start_block=0,
            end_block_exclusive=1,
        )


@pytest.mark.asyncio
async def test_scanner_rejects_out_of_scope_response_fields() -> None:
    rpc = FakeLogsRpc([rpc_log(1, address=OTHER_TOKEN)])
    scanner = StrictLogScanner(cast(Any, rpc), initial_chunk_size=10)

    with pytest.raises(DiscoveryResponseError, match="unexpected contract address"):
        await scanner.scan(
            token_address=TOKEN,
            topic0=TOPIC0,
            start_block=0,
            end_block_exclusive=2,
        )


@pytest.mark.asyncio
async def test_exact_duplicate_logs_are_deduplicated_deterministically() -> None:
    rpc = FakeLogsRpc([rpc_log(2)], duplicate_results=True)
    scanner = StrictLogScanner(cast(Any, rpc), initial_chunk_size=10)

    result = await scanner.scan(
        token_address=TOKEN,
        topic0=TOPIC0,
        start_block=0,
        end_block_exclusive=3,
    )

    assert len(result.logs) == 1
    assert result.logs[0].block_number == 2


def test_coverage_frontier_starts_at_registry_start_and_exposes_prefix_hole() -> None:
    ranges = [BlockRange(12, 20), BlockRange(20, 30)]

    assert contiguous_frontier(10, ranges) == 10
    assert coverage_gaps(BlockRange(10, 30), ranges) == (BlockRange(10, 12),)
    assert merge_coverage(ranges) == (BlockRange(12, 30),)


@pytest.mark.asyncio
async def test_holder_extraction_uses_configured_topics_and_excludes_zero() -> None:
    log = rpc_log(1, topics=[TOPIC0, ZERO_WORD, HOLDER_WORD])
    rpc = FakeLogsRpc([log])
    scanner = StrictLogScanner(cast(Any, rpc))

    result = await scanner.scan(
        token_address=TOKEN,
        topic0=TOPIC0,
        start_block=1,
        end_block_exclusive=2,
    )

    assert extract_holders(result.logs, [1, 2]) == (HOLDER,)
