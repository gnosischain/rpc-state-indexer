"""Minimal committed Multicall3 ABI encoding and decoding."""

from __future__ import annotations

from collections.abc import Sequence

from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode

from rpc_state_indexer.evm.calldata import AGGREGATE3_SELECTOR


def encode_aggregate3(calls: Sequence[tuple[str, bool, bytes]]) -> bytes:
    return AGGREGATE3_SELECTOR + abi_encode(["(address,bool,bytes)[]"], [list(calls)])


def decode_aggregate3(returndata: bytes) -> tuple[tuple[bool, bytes], ...]:
    if not returndata:
        raise ValueError("aggregate3 returned empty data")
    try:
        (decoded,) = abi_decode(["(bool,bytes)[]"], returndata)
    except Exception as exc:
        raise ValueError("malformed aggregate3 return data") from exc
    return tuple((bool(success), bytes(data)) for success, data in decoded)
