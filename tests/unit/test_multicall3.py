import asyncio
from typing import Any, cast

import pytest
from eth_abi.abi import decode as abi_decode

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.evm.abi import encode_aggregate3
from rpc_state_indexer.evm.calldata import (
    AGGREGATE3_SELECTOR,
    GET_BLOCK_NUMBER_SELECTOR,
)
from rpc_state_indexer.execution.base import ContractCall
from rpc_state_indexer.execution.errors import SentinelMismatch
from rpc_state_indexer.execution.multicall3 import Multicall3Executor

ANCHOR = BlockRef(100, "0x" + "11" * 32, "0x" + "22" * 32, 123456)


def word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def sentinels(
    block: int = 100,
    timestamp: int = 123456,
    parent: bytes = bytes.fromhex("22" * 32),
) -> tuple[tuple[bool, bytes], ...]:
    return ((True, word(block)), (True, word(timestamp)), (True, parent))


def test_aggregate3_encoding_uses_canonical_selector_and_tuple_shape() -> None:
    target = "0x" + "ab" * 20
    encoded = encode_aggregate3([(target, False, GET_BLOCK_NUMBER_SELECTOR)])
    assert encoded[:4] == AGGREGATE3_SELECTOR
    (calls,) = abi_decode(["(address,bool,bytes)[]"], encoded[4:])
    assert calls[0][0].lower() == target
    assert calls[0][1] is False
    assert calls[0][2] == GET_BLOCK_NUMBER_SELECTOR


def test_sentinel_triple_accepts_exact_anchor() -> None:
    Multicall3Executor._verify_sentinels(sentinels(), ANCHOR, "head")


@pytest.mark.parametrize(
    "values",
    [
        sentinels(block=101),
        sentinels(timestamp=123457),
        sentinels(parent=b"\x33" * 32),
        ((False, b""),) + sentinels()[1:],
    ],
)
def test_sentinel_triple_fails_closed(
    values: tuple[tuple[bool, bytes], ...],
) -> None:
    with pytest.raises(SentinelMismatch):
        Multicall3Executor._verify_sentinels(values, ANCHOR, "tail")


# --------------------------------------------------- batch parallelism (execute)


def _fake_batch(index: int) -> object:
    """Stand-in for a VerifiedBatchResult; execute() only orders and concatenates."""

    return f"batch-{index}"


def _executor(*, batch_size: int, max_parallel: int) -> Multicall3Executor:
    return Multicall3Executor(
        cast(Any, object()),
        address="0x" + "ca" * 20,
        deployment_block=0,
        batch_size=batch_size,
        max_parallel_batches=max_parallel,
    )


def _calls(count: int) -> list[ContractCall]:
    return [ContractCall(f"k{i}", "0x" + "11" * 20, b"\x00" * 4) for i in range(count)]


@pytest.mark.asyncio
async def test_independent_batches_run_concurrently() -> None:
    """Each batch proves itself with its own sentinels, so they need not be serialised.

    Running them one at a time left the RPC client's concurrency semaphore idle and made a
    full-holder census cost tens of seconds of pure round-trip latency.
    """

    subject = _executor(batch_size=1, max_parallel=4)
    in_flight = 0
    peak = 0

    async def fake_adaptive(group, anchor):  # type: ignore[no-untyped-def]
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)  # yield so siblings can start
        in_flight -= 1
        return [group[0].key]

    subject._execute_adaptive = fake_adaptive  # type: ignore[method-assign]
    result = cast(Any, await subject.execute(_calls(4), ANCHOR))

    assert peak > 1, "batches were still executed one at a time"
    assert result == ["k0", "k1", "k2", "k3"], "gather must preserve batch order"


@pytest.mark.asyncio
async def test_parallelism_is_bounded_by_max_parallel_batches() -> None:
    subject = _executor(batch_size=1, max_parallel=2)
    in_flight = 0
    peak = 0

    async def fake_adaptive(group, anchor):  # type: ignore[no-untyped-def]
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return [group[0].key]

    subject._execute_adaptive = fake_adaptive  # type: ignore[method-assign]
    result = cast(Any, await subject.execute(_calls(6), ANCHOR))

    assert peak <= 2, f"exceeded the configured wave bound (peak={peak})"
    assert result == [f"k{i}" for i in range(6)]


@pytest.mark.asyncio
async def test_single_batch_takes_the_direct_path() -> None:
    subject = _executor(batch_size=250, max_parallel=8)
    seen: list[int] = []

    async def fake_adaptive(group, anchor):  # type: ignore[no-untyped-def]
        seen.append(len(group))
        return ["only"]

    subject._execute_adaptive = fake_adaptive  # type: ignore[method-assign]
    assert cast(Any, await subject.execute(_calls(10), ANCHOR)) == ["only"]
    assert seen == [10]


def test_rejects_non_positive_parallelism() -> None:
    with pytest.raises(ValueError):
        _executor(batch_size=1, max_parallel=0)
