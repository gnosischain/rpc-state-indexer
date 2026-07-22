import pytest
from eth_abi.abi import decode as abi_decode

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.evm.abi import encode_aggregate3
from rpc_state_indexer.evm.calldata import (
    AGGREGATE3_SELECTOR,
    GET_BLOCK_NUMBER_SELECTOR,
)
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
