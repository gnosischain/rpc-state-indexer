from typing import Any, cast

import pytest

from rpc_state_indexer.rpc.capabilities import probe_archive_state, probe_eip1898
from rpc_state_indexer.rpc.errors import RpcResponseError


class FakeEndpoint:
    supports_eip1898 = False
    archive_from_block: int | None = None


class FakeRpc:
    def __init__(self, canonical_block: int | RpcResponseError) -> None:
        self.canonical_block = canonical_block
        self.calls = 0

    async def call_on_endpoint(
        self, endpoint: object, method: str, params: list[object]
    ) -> str:
        del endpoint, method, params
        self.calls += 1
        if self.calls == 1:
            if isinstance(self.canonical_block, RpcResponseError):
                raise self.canonical_block
            return "0x" + self.canonical_block.to_bytes(32, "big").hex()
        raise RpcResponseError(-32000, "header not found")


@pytest.mark.asyncio
async def test_eip1898_requires_exact_canonical_value_and_negative_probe() -> None:
    endpoint = FakeEndpoint()
    supported = await probe_eip1898(
        cast(Any, FakeRpc(100)),
        cast(Any, endpoint),
        call_to="0x" + "11" * 20,
        canonical_block_hash="0x" + "22" * 32,
        canonical_block_number=100,
        calldata="0x42cbb15c",
    )
    assert supported is True
    assert endpoint.supports_eip1898 is True


@pytest.mark.asyncio
async def test_proxy_serving_latest_does_not_pass_eip1898_probe() -> None:
    endpoint = FakeEndpoint()
    supported = await probe_eip1898(
        cast(Any, FakeRpc(101)),
        cast(Any, endpoint),
        call_to="0x" + "11" * 20,
        canonical_block_hash="0x" + "22" * 32,
        canonical_block_number=100,
        calldata="0x42cbb15c",
    )
    assert supported is False
    assert endpoint.supports_eip1898 is False


@pytest.mark.asyncio
async def test_invalid_params_on_positive_probe_is_not_support() -> None:
    endpoint = FakeEndpoint()
    supported = await probe_eip1898(
        cast(Any, FakeRpc(RpcResponseError(-32602, "invalid params"))),
        cast(Any, endpoint),
        call_to="0x" + "11" * 20,
        canonical_block_hash="0x" + "22" * 32,
        canonical_block_number=100,
        calldata="0x42cbb15c",
    )
    assert supported is False


class ArchiveRpc:
    def __init__(self, state_result: str = "0x" + "00" * 31 + "01") -> None:
        self.state_result = state_result
        self.calls: list[tuple[str, list[object]]] = []

    async def call_on_endpoint(
        self,
        endpoint: object,
        method: str,
        params: list[object],
    ) -> object:
        del endpoint
        self.calls.append((method, params))
        if method == "eth_getBlockByNumber":
            return {
                "number": "0xa",
                "timestamp": "0x64",
                "hash": "0x" + "11" * 32,
                "parentHash": "0x" + "22" * 32,
            }
        if method == "eth_getCode":
            return "0x6000"
        if method == "eth_call":
            return self.state_result
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_archive_probe_uses_hash_reference_and_records_earliest_block() -> None:
    endpoint = FakeEndpoint()
    endpoint.supports_eip1898 = True
    rpc = ArchiveRpc()

    await probe_archive_state(
        cast(Any, rpc),
        cast(Any, endpoint),
        contract_address="0x" + "33" * 20,
        block_number=10,
        calldata="0x18160ddd",
    )

    assert endpoint.archive_from_block == 10
    assert rpc.calls[1][1][1] == {
        "blockHash": "0x" + "11" * 32,
        "requireCanonical": True,
    }
    assert rpc.calls[2][1][1] == rpc.calls[1][1][1]


@pytest.mark.asyncio
async def test_archive_probe_rejects_empty_state_return() -> None:
    endpoint = FakeEndpoint()
    rpc = ArchiveRpc("0x")

    with pytest.raises(ValueError, match="empty_return"):
        await probe_archive_state(
            cast(Any, rpc),
            cast(Any, endpoint),
            contract_address="0x" + "33" * 20,
            block_number=10,
            calldata="0x18160ddd",
        )

    assert endpoint.archive_from_block is None
