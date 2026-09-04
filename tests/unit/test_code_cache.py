"""HistoricalCodeVerifier caches evidence per (address, anchor, expected hash).

Before the cache, the Multicall3 contract was re-read with an identical
eth_getCode once per target — ~3,400 times per date.
"""

from typing import Any

import pytest

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.execution.code import HistoricalCodeVerifier, RuntimeCodeHashMismatch

ANCHOR_A = BlockRef(100, "0x" + "aa" * 32, "0x" + "00" * 32, 1000)
ANCHOR_B = BlockRef(200, "0x" + "bb" * 32, "0x" + "aa" * 32, 2000)
CODE = "0x6001"  # any non-empty runtime code


class FakeEndpoint:
    provider_group = "internal"


class FakePool:
    async def select(self, **_: Any) -> FakeEndpoint:
        return FakeEndpoint()


class FakeRpc:
    def __init__(self) -> None:
        self.endpoint_pool = FakePool()
        self.calls: list[tuple[str, str]] = []

    async def call_on_endpoint(self, _endpoint: Any, method: str, params: list[Any]) -> str:
        self.calls.append((method, params[0]))
        return CODE


@pytest.mark.asyncio
async def test_same_address_and_anchor_is_read_once() -> None:
    rpc = FakeRpc()
    verifier = HistoricalCodeVerifier(rpc)  # type: ignore[arg-type]

    first = await verifier.verify("0xABC", ANCHOR_A)
    second = await verifier.verify("0xabc", ANCHOR_A)  # case-insensitive key

    assert first == second
    assert rpc.calls == [("eth_getCode", "0xabc")]


@pytest.mark.asyncio
async def test_new_anchor_reads_again_and_drops_the_old_bucket() -> None:
    rpc = FakeRpc()
    verifier = HistoricalCodeVerifier(rpc)  # type: ignore[arg-type]

    await verifier.verify("0xabc", ANCHOR_A)
    await verifier.verify("0xabc", ANCHOR_B)
    await verifier.verify("0xabc", ANCHOR_A)  # A's bucket was dropped -> reads again

    assert [p for _, p in rpc.calls] == ["0xabc", "0xabc", "0xabc"]
    assert set(verifier._cache) == {ANCHOR_A.block_hash}  # only the current anchor kept


@pytest.mark.asyncio
async def test_failures_are_not_cached() -> None:
    rpc = FakeRpc()
    verifier = HistoricalCodeVerifier(rpc)  # type: ignore[arg-type]

    with pytest.raises(RuntimeCodeHashMismatch):
        await verifier.verify("0xabc", ANCHOR_A, expected_code_hash="0x" + "ff" * 32)
    with pytest.raises(RuntimeCodeHashMismatch):
        await verifier.verify("0xabc", ANCHOR_A, expected_code_hash="0x" + "ff" * 32)

    assert len(rpc.calls) == 2  # re-read, not remembered as a verdict
