from __future__ import annotations

import re
from collections.abc import Mapping

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.execution.errors import AnchorHashMismatch
from rpc_state_indexer.rpc.client import AsyncRpcClient
from rpc_state_indexer.rpc.endpoint import RpcEndpoint

HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def normalize_hash(value: object) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise AnchorHashMismatch("invalid block hash")
    return value.lower()


def eip1898_reference(anchor: BlockRef) -> dict[str, object]:
    return {"blockHash": normalize_hash(anchor.block_hash), "requireCanonical": True}


def number_reference(anchor: BlockRef) -> str:
    return hex(anchor.number)


async def read_block_hash(
    rpc: AsyncRpcClient,
    endpoint: RpcEndpoint,
    block_number: int,
) -> str:
    result = await rpc.call_on_endpoint(
        endpoint,
        "eth_getBlockByNumber",
        [hex(block_number), False],
    )
    if not isinstance(result, Mapping):
        raise AnchorHashMismatch("block lookup returned no object")
    return normalize_hash(result.get("hash"))


async def assert_anchor_hash(
    rpc: AsyncRpcClient,
    endpoint: RpcEndpoint,
    anchor: BlockRef,
) -> None:
    observed = await read_block_hash(rpc, endpoint, anchor.number)
    if observed != normalize_hash(anchor.block_hash):
        raise AnchorHashMismatch(f"endpoint {endpoint.name} anchor hash mismatch")

