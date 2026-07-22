"""Resolve immutable UTC-day anchors from canonical JSON-RPC block data.

No block-time estimate is used here.  Resolution binary-searches actual block
timestamps below a finalized (or explicitly confirmation-depth-safe) tip, then
proves the boundary with the adjacent block and its parent hash.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.errors import (
    AnchorConflict,
    AnchorNotFinalized,
    AnchorValidationError,
)
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.errors import RpcResponseError

_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX_QUANTITY_RE = re.compile(r"^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$")


class RpcCaller(Protocol):
    async def call(
        self,
        method: str,
        params: Sequence[Any],
        *,
        historical_block: int | None = None,
        require_eip1898: bool = False,
    ) -> tuple[Any, RpcEndpoint]: ...


class _FinalityTagUnavailable(RuntimeError):
    """The endpoint understood JSON-RPC but has no value for the configured tag."""


@dataclass(frozen=True, slots=True)
class ResolvedAnchor:
    """A day boundary proven by two adjacent canonical blocks."""

    snapshot_date: date
    anchor: BlockRef
    next_block: BlockRef
    safe_tip: BlockRef
    finality_source: str
    endpoint_fingerprints: tuple[str, ...]

    @property
    def boundary_timestamp(self) -> int:
        return utc_day_end_timestamp(self.snapshot_date)


def utc_day_end_timestamp(snapshot_date: date) -> int:
    """Return midnight immediately after ``snapshot_date`` as a UTC epoch."""

    boundary = datetime.combine(snapshot_date + timedelta(days=1), time.min, tzinfo=UTC)
    return int(boundary.timestamp())


def _parse_quantity(value: object, field: str) -> int:
    if not isinstance(value, str) or not _HEX_QUANTITY_RE.fullmatch(value):
        raise AnchorValidationError(f"block {field} is not a canonical hex quantity")
    return int(value, 16)


def _parse_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise AnchorValidationError(f"block {field} is not a 32-byte hash")
    return value.lower()


def parse_block(value: object, *, expected_number: int | None = None) -> BlockRef:
    """Strictly decode the subset of a block needed for anchoring."""

    if not isinstance(value, Mapping):
        raise AnchorValidationError("block lookup returned no block object")
    number = _parse_quantity(value.get("number"), "number")
    timestamp = _parse_quantity(value.get("timestamp"), "timestamp")
    block_hash = _parse_hash(value.get("hash"), "hash")
    parent_hash = _parse_hash(value.get("parentHash"), "parentHash")
    if expected_number is not None and number != expected_number:
        raise AnchorValidationError(
            f"requested block {expected_number}, endpoint returned block {number}"
        )
    return BlockRef(number, block_hash, parent_hash, timestamp)


def validate_resolved_anchor(resolved: ResolvedAnchor) -> None:
    """Check the complete immutable boundary proof."""

    boundary = resolved.boundary_timestamp
    anchor = resolved.anchor
    next_block = resolved.next_block
    if anchor.timestamp >= boundary:
        raise AnchorValidationError("anchor timestamp is not before the UTC boundary")
    if next_block.timestamp < boundary:
        raise AnchorValidationError("block after anchor is still before the UTC boundary")
    if next_block.number != anchor.number + 1:
        raise AnchorValidationError("anchor proof blocks are not adjacent")
    if next_block.parent_hash != anchor.block_hash:
        raise AnchorValidationError("block after anchor does not reference anchor hash")
    if next_block.number > resolved.safe_tip.number:
        raise AnchorNotFinalized("UTC boundary is newer than the safe block tip")


def assert_anchor_immutable(existing: BlockRef, candidate: BlockRef) -> None:
    """Reject a second resolution that changes an already stored day anchor."""

    if existing != candidate:
        raise AnchorConflict(
            "stored day anchor differs from newly resolved block; manual reorg review required"
        )


class AnchorResolver:
    """Resolve day-end blocks using exact chain timestamps."""

    def __init__(
        self,
        rpc: RpcCaller,
        *,
        finality_tag: str = "finalized",
        fallback_confirmation_depth: int = 64,
    ) -> None:
        if not finality_tag:
            raise ValueError("finality_tag cannot be empty")
        if fallback_confirmation_depth < 1:
            raise ValueError("fallback_confirmation_depth must be positive")
        self._rpc = rpc
        self._finality_tag = finality_tag
        self._confirmation_depth = fallback_confirmation_depth

    async def _read_by_tag(self, tag: str) -> tuple[BlockRef, RpcEndpoint]:
        raw, endpoint = await self._rpc.call("eth_getBlockByNumber", [tag, False])
        if raw is None and tag != "latest":
            raise _FinalityTagUnavailable(tag)
        return parse_block(raw), endpoint

    async def _read_by_number(self, number: int) -> tuple[BlockRef, RpcEndpoint]:
        # Block *header* reads are available on any full node and must not be gated by an
        # endpoint's archive-state floor (`archive_from_block`). Anchor resolution reads
        # genesis (block 0) to bound its binary search; gating that read by the earliest
        # token deployment block would leave no endpoint able to serve it. Archive gating
        # applies only to historical *state* calls (eth_call/eth_getCode), not headers.
        raw, endpoint = await self._rpc.call(
            "eth_getBlockByNumber",
            [hex(number), False],
        )
        return parse_block(raw, expected_number=number), endpoint

    async def _safe_tip(self) -> tuple[BlockRef, str, tuple[str, ...]]:
        """Prefer a consensus finality tag; use depth only when the tag is unsupported."""

        try:
            finalized, endpoint = await self._read_by_tag(self._finality_tag)
        except (RpcResponseError, _FinalityTagUnavailable) as exc:
            # An unsupported block tag is an application-level JSON-RPC rejection.
            # Transport exhaustion deliberately does not downgrade finality guarantees.
            latest, latest_endpoint = await self._read_by_tag("latest")
            if latest.number < self._confirmation_depth:
                raise AnchorNotFinalized(
                    "chain height is below configured confirmation depth"
                ) from exc
            safe_number = latest.number - self._confirmation_depth
            safe, safe_endpoint = await self._read_by_number(safe_number)
            fingerprints = tuple(
                dict.fromkeys(
                    (latest_endpoint.fingerprint, safe_endpoint.fingerprint)
                )
            )
            return safe, f"confirmations:{self._confirmation_depth}", fingerprints
        return finalized, f"tag:{self._finality_tag}", (endpoint.fingerprint,)

    async def resolve(self, snapshot_date: date) -> ResolvedAnchor:
        boundary = utc_day_end_timestamp(snapshot_date)
        safe_tip, finality_source, safety_endpoints = await self._safe_tip()
        if safe_tip.timestamp < boundary:
            raise AnchorNotFinalized(
                "safe chain tip has not reached the end of the requested UTC day"
            )

        fingerprints = list(safety_endpoints)
        cache: dict[int, BlockRef] = {safe_tip.number: safe_tip}

        async def read(number: int) -> BlockRef:
            cached = cache.get(number)
            if cached is not None:
                return cached
            block, endpoint = await self._read_by_number(number)
            cache[number] = block
            if endpoint.fingerprint not in fingerprints:
                fingerprints.append(endpoint.fingerprint)
            return block

        genesis = await read(0)
        if genesis.timestamp >= boundary:
            raise AnchorValidationError("requested UTC day ends before chain genesis")

        # Invariant: low timestamp < boundary, high timestamp >= boundary.
        low = 0
        high = safe_tip.number
        while high - low > 1:
            middle = low + (high - low) // 2
            candidate = await read(middle)
            if candidate.timestamp < boundary:
                low = middle
            else:
                high = middle

        anchor = await read(low)
        next_block = await read(high)

        # Re-read the two proof blocks after the search.  This catches a proxy
        # changing views mid-resolution instead of persisting a mixed-fork pair.
        confirmed_anchor, anchor_endpoint = await self._read_by_number(anchor.number)
        confirmed_next, next_endpoint = await self._read_by_number(next_block.number)
        if confirmed_anchor != anchor or confirmed_next != next_block:
            raise AnchorConflict("canonical blocks changed during anchor resolution")
        for endpoint in (anchor_endpoint, next_endpoint):
            if endpoint.fingerprint not in fingerprints:
                fingerprints.append(endpoint.fingerprint)
        resolved = ResolvedAnchor(
            snapshot_date=snapshot_date,
            anchor=confirmed_anchor,
            next_block=confirmed_next,
            safe_tip=safe_tip,
            finality_source=finality_source,
            endpoint_fingerprints=tuple(fingerprints),
        )
        validate_resolved_anchor(resolved)
        return resolved


__all__ = [
    "AnchorResolver",
    "ResolvedAnchor",
    "assert_anchor_immutable",
    "parse_block",
    "utc_day_end_timestamp",
    "validate_resolved_anchor",
]
