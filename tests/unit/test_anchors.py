from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, cast

import pytest

from rpc_state_indexer.core.anchors import (
    AnchorResolver,
    assert_anchor_immutable,
    parse_block,
    utc_day_end_timestamp,
)
from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.errors import (
    AnchorConflict,
    AnchorNotFinalized,
    AnchorValidationError,
)
from rpc_state_indexer.rpc.errors import RpcResponseError


class FakeEndpoint:
    fingerprint = "endpoint-a"


def block_hash(number: int) -> str:
    return f"0x{number + 1:064x}"


def rpc_block(number: int, timestamp: int, *, parent_hash: str | None = None) -> dict[str, str]:
    if parent_hash is None:
        parent_hash = "0x" + "00" * 32 if number == 0 else block_hash(number - 1)
    return {
        "number": hex(number),
        "hash": block_hash(number),
        "parentHash": parent_hash,
        "timestamp": hex(timestamp),
    }


class FakeBlockRpc:
    def __init__(
        self,
        blocks: dict[int, dict[str, str]],
        *,
        safe_number: int,
        finality_supported: bool = True,
        latest_number: int | None = None,
    ) -> None:
        self.blocks = blocks
        self.safe_number = safe_number
        self.finality_supported = finality_supported
        self.latest_number = latest_number if latest_number is not None else max(blocks)
        self.calls: list[str] = []

    async def call(
        self,
        method: str,
        params: Sequence[Any],
        *,
        historical_block: int | None = None,
        require_eip1898: bool = False,
    ) -> tuple[Any, FakeEndpoint]:
        del historical_block, require_eip1898
        assert method == "eth_getBlockByNumber"
        tag = cast(str, params[0])
        self.calls.append(tag)
        if tag == "finalized":
            if not self.finality_supported:
                raise RpcResponseError(-32602, "unsupported block tag finalized")
            return self.blocks[self.safe_number], FakeEndpoint()
        if tag == "latest":
            return self.blocks[self.latest_number], FakeEndpoint()
        return self.blocks[int(tag, 16)], FakeEndpoint()


def boundary_blocks(*, bad_parent: bool = False) -> tuple[date, dict[int, dict[str, str]]]:
    snapshot_date = date(2024, 1, 1)
    boundary = utc_day_end_timestamp(snapshot_date)
    blocks = {
        0: rpc_block(0, boundary - 100),
        1: rpc_block(1, boundary - 50),
        2: rpc_block(2, boundary - 1),
        3: rpc_block(
            3,
            boundary,
            parent_hash="0x" + "ff" * 32 if bad_parent else None,
        ),
        4: rpc_block(4, boundary + 10),
        5: rpc_block(5, boundary + 20),
        6: rpc_block(6, boundary + 30),
    }
    return snapshot_date, blocks


@pytest.mark.asyncio
async def test_anchor_is_last_block_strictly_before_utc_midnight() -> None:
    snapshot_date, blocks = boundary_blocks()
    rpc = FakeBlockRpc(blocks, safe_number=4)

    resolved = await AnchorResolver(cast(Any, rpc)).resolve(snapshot_date)

    assert resolved.anchor.number == 2
    assert resolved.anchor.timestamp == utc_day_end_timestamp(snapshot_date) - 1
    assert resolved.next_block.number == 3
    assert resolved.next_block.timestamp == utc_day_end_timestamp(snapshot_date)
    assert resolved.finality_source == "tag:finalized"
    assert resolved.endpoint_fingerprints == ("endpoint-a",)


@pytest.mark.asyncio
async def test_anchor_falls_back_to_explicit_confirmation_depth() -> None:
    snapshot_date, blocks = boundary_blocks()
    rpc = FakeBlockRpc(
        blocks,
        safe_number=4,
        finality_supported=False,
        latest_number=6,
    )

    resolved = await AnchorResolver(
        cast(Any, rpc), fallback_confirmation_depth=2
    ).resolve(snapshot_date)

    assert resolved.safe_tip.number == 4
    assert resolved.finality_source == "confirmations:2"
    assert "latest" in rpc.calls


@pytest.mark.asyncio
async def test_anchor_refuses_day_not_reached_by_safe_tip() -> None:
    snapshot_date, blocks = boundary_blocks()
    rpc = FakeBlockRpc(blocks, safe_number=2)

    with pytest.raises(AnchorNotFinalized, match="has not reached"):
        await AnchorResolver(cast(Any, rpc)).resolve(snapshot_date)


@pytest.mark.asyncio
async def test_anchor_rejects_broken_parent_link() -> None:
    snapshot_date, blocks = boundary_blocks(bad_parent=True)
    rpc = FakeBlockRpc(blocks, safe_number=4)

    with pytest.raises(AnchorValidationError, match="does not reference"):
        await AnchorResolver(cast(Any, rpc)).resolve(snapshot_date)


def test_anchor_immutability_rejects_drift() -> None:
    existing = BlockRef(10, block_hash(10), block_hash(9), 123)
    assert_anchor_immutable(existing, existing)

    changed = BlockRef(10, "0x" + "aa" * 32, block_hash(9), 123)
    with pytest.raises(AnchorConflict, match="stored day anchor differs"):
        assert_anchor_immutable(existing, changed)


def test_block_parser_rejects_noncanonical_hex_quantities() -> None:
    value = rpc_block(1, 100)
    value["number"] = "0x01"

    with pytest.raises(AnchorValidationError, match="canonical hex quantity"):
        parse_block(value)
