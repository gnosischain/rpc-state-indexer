"""Gap-free event discovery over strict half-open block ranges."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from rpc_state_indexer.errors import DiscoveryError
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.errors import RpcProviderLimit

_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX_QUANTITY_RE = re.compile(r"^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$")
_HEX_DATA_RE = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


class RpcCaller(Protocol):
    async def call(
        self,
        method: str,
        params: Sequence[Any],
        *,
        historical_block: int | None = None,
        require_eip1898: bool = False,
    ) -> tuple[Any, RpcEndpoint]: ...


class DiscoveryRangeFailed(DiscoveryError):
    """A requested range could not be proven complete."""


class DiscoveryResponseError(DiscoveryError):
    """A provider returned malformed or out-of-scope log data."""


@dataclass(frozen=True, order=True, slots=True)
class BlockRange:
    start: int
    end_exclusive: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("block range start cannot be negative")
        if self.end_exclusive <= self.start:
            raise ValueError("block range must be non-empty")

    @property
    def size(self) -> int:
        return self.end_exclusive - self.start

    def split(self) -> tuple[BlockRange, BlockRange]:
        if self.size < 2:
            raise ValueError("a one-block range cannot be split")
        middle = self.start + self.size // 2
        return BlockRange(self.start, middle), BlockRange(middle, self.end_exclusive)


@dataclass(frozen=True, slots=True)
class NormalizedLog:
    address: str
    topic0: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    block_hash: str
    transaction_hash: str
    transaction_index: int
    log_index: int

    @property
    def identity(self) -> tuple[str, str, int]:
        return self.block_hash, self.transaction_hash, self.log_index

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        return (
            self.block_number,
            self.transaction_index,
            self.log_index,
            self.transaction_hash,
        )

    def holders(
        self,
        topic_positions: Iterable[int],
        *,
        include_zero: bool = False,
    ) -> tuple[str, ...]:
        addresses: list[str] = []
        for position in topic_positions:
            if position < 1 or position >= len(self.topics):
                raise DiscoveryResponseError(
                    f"holder topic position {position} missing from log"
                )
            topic = self.topics[position]
            # Indexed addresses are left-padded to a full ABI word.  Reject a
            # non-canonical word instead of silently taking its last 20 bytes.
            if topic[2:26] != "0" * 24:
                raise DiscoveryResponseError("indexed holder is not a canonical address word")
            address = "0x" + topic[-40:].lower()
            if include_zero or address != ZERO_ADDRESS:
                addresses.append(address)
        return tuple(addresses)


@dataclass(frozen=True, slots=True)
class CompletedRange:
    block_range: BlockRange
    log_count: int
    endpoint_fingerprint: str


@dataclass(frozen=True, slots=True)
class DiscoveryScan:
    requested_range: BlockRange
    completed_ranges: tuple[CompletedRange, ...]
    logs: tuple[NormalizedLog, ...]

    @property
    def frontier(self) -> int:
        return contiguous_frontier(
            self.requested_range.start,
            (item.block_range for item in self.completed_ranges),
        )


def _parse_quantity(value: object, field: str) -> int:
    if not isinstance(value, str) or not _HEX_QUANTITY_RE.fullmatch(value):
        raise DiscoveryResponseError(f"log {field} is not a canonical hex quantity")
    return int(value, 16)


def _parse_hash(value: object, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise DiscoveryResponseError(f"log {field} is not a 32-byte hash")
    return value.lower()


def _parse_log(
    value: object,
    *,
    expected_address: str,
    expected_topic0: str,
    requested_range: BlockRange,
) -> NormalizedLog:
    if not isinstance(value, Mapping):
        raise DiscoveryResponseError("eth_getLogs entry must be an object")
    address = value.get("address")
    if not isinstance(address, str) or address.lower() != expected_address:
        raise DiscoveryResponseError("eth_getLogs returned an unexpected contract address")
    removed = value.get("removed")
    if type(removed) is not bool:
        raise DiscoveryResponseError("log removed flag must be a boolean")
    if removed:
        raise DiscoveryResponseError("eth_getLogs returned a removed log for finalized history")

    raw_topics = value.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise DiscoveryResponseError("eth_getLogs entry has no topics")
    topics = tuple(_parse_hash(topic, "topic") for topic in raw_topics)
    if topics[0] != expected_topic0:
        raise DiscoveryResponseError("eth_getLogs returned an unexpected topic0")

    data = value.get("data")
    if not isinstance(data, str) or not _HEX_DATA_RE.fullmatch(data):
        raise DiscoveryResponseError("log data is not even-length hex")

    block_number = _parse_quantity(value.get("blockNumber"), "blockNumber")
    if not requested_range.start <= block_number < requested_range.end_exclusive:
        raise DiscoveryResponseError("eth_getLogs returned a block outside the requested range")
    transaction_index = _parse_quantity(
        value.get("transactionIndex"), "transactionIndex"
    )
    log_index = _parse_quantity(value.get("logIndex"), "logIndex")
    return NormalizedLog(
        address=expected_address,
        topic0=expected_topic0,
        topics=topics,
        data=data.lower(),
        block_number=block_number,
        block_hash=_parse_hash(value.get("blockHash"), "blockHash"),
        transaction_hash=_parse_hash(value.get("transactionHash"), "transactionHash"),
        transaction_index=transaction_index,
        log_index=log_index,
    )


def deduplicate_logs(logs: Iterable[NormalizedLog]) -> tuple[NormalizedLog, ...]:
    """Deduplicate exact provider duplicates and reject identity collisions."""

    by_identity: dict[tuple[str, str, int], NormalizedLog] = {}
    for log in logs:
        previous = by_identity.get(log.identity)
        if previous is not None and previous != log:
            raise DiscoveryResponseError("conflicting logs share one canonical identity")
        by_identity[log.identity] = log
    return tuple(sorted(by_identity.values(), key=lambda item: item.sort_key))


def merge_coverage(ranges: Iterable[BlockRange]) -> tuple[BlockRange, ...]:
    """Return the union of completed half-open ranges."""

    ordered = sorted(ranges)
    if not ordered:
        return ()
    merged: list[BlockRange] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end_exclusive:
            merged[-1] = BlockRange(
                previous.start,
                max(previous.end_exclusive, current.end_exclusive),
            )
        else:
            merged.append(current)
    return tuple(merged)


def contiguous_frontier(coverage_start: int, ranges: Iterable[BlockRange]) -> int:
    """Return the first uncovered block starting from the configured registry start."""

    if coverage_start < 0:
        raise ValueError("coverage_start cannot be negative")
    frontier = coverage_start
    for block_range in merge_coverage(ranges):
        if block_range.end_exclusive <= frontier:
            continue
        if block_range.start > frontier:
            break
        frontier = block_range.end_exclusive
    return frontier


def coverage_gaps(
    required_range: BlockRange,
    completed_ranges: Iterable[BlockRange],
) -> tuple[BlockRange, ...]:
    """List uncovered subranges within a required half-open range."""

    gaps: list[BlockRange] = []
    cursor = required_range.start
    for covered in merge_coverage(completed_ranges):
        if covered.end_exclusive <= required_range.start:
            continue
        if covered.start >= required_range.end_exclusive:
            break
        clipped_start = max(covered.start, required_range.start)
        clipped_end = min(covered.end_exclusive, required_range.end_exclusive)
        if clipped_start > cursor:
            gaps.append(BlockRange(cursor, clipped_start))
        cursor = max(cursor, clipped_end)
        if cursor >= required_range.end_exclusive:
            break
    if cursor < required_range.end_exclusive:
        gaps.append(BlockRange(cursor, required_range.end_exclusive))
    return tuple(gaps)


def extract_holders(
    logs: Iterable[NormalizedLog],
    topic_positions: Iterable[int],
    *,
    include_zero: bool = False,
) -> tuple[str, ...]:
    """Extract a deterministic unique holder set from configured indexed topics."""

    positions = tuple(topic_positions)
    if not positions or any(position < 1 for position in positions):
        raise ValueError("holder topic positions must be positive")
    holders = {
        holder
        for log in logs
        for holder in log.holders(positions, include_zero=include_zero)
    }
    return tuple(sorted(holders))


class StrictLogScanner:
    """Scan an event stream without ever skipping a failing block."""

    def __init__(
        self,
        rpc: RpcCaller,
        *,
        initial_chunk_size: int = 10_000,
        provider_result_cap: int = 10_000,
    ) -> None:
        if initial_chunk_size < 1 or provider_result_cap < 1:
            raise ValueError("chunk size and provider result cap must be positive")
        self._rpc = rpc
        self._chunk_size = initial_chunk_size
        self._result_cap = provider_result_cap

    async def _read_range(
        self,
        token_address: str,
        topic0: str,
        block_range: BlockRange,
    ) -> tuple[list[object], RpcEndpoint]:
        params = [
            {
                "address": token_address,
                "topics": [topic0],
                "fromBlock": hex(block_range.start),
                "toBlock": hex(block_range.end_exclusive - 1),
            }
        ]
        raw, endpoint = await self._rpc.call(
            "eth_getLogs",
            params,
            historical_block=block_range.end_exclusive - 1,
        )
        if not isinstance(raw, list):
            raise DiscoveryResponseError("eth_getLogs response must be an array")
        return raw, endpoint

    async def scan(
        self,
        *,
        token_address: str,
        topic0: str,
        start_block: int,
        end_block_exclusive: int,
    ) -> DiscoveryScan:
        if not _ADDRESS_RE.fullmatch(token_address):
            raise ValueError("token_address must be normalized lowercase 0x hex")
        normalized_topic0 = _parse_hash(topic0, "topic0")
        requested = BlockRange(start_block, end_block_exclusive)

        pending: list[BlockRange] = []
        cursor = requested.start
        while cursor < requested.end_exclusive:
            chunk_end = min(cursor + self._chunk_size, requested.end_exclusive)
            pending.append(BlockRange(cursor, chunk_end))
            cursor = chunk_end
        # Pop the lowest range first while making split order deterministic.
        pending.reverse()

        completed: list[CompletedRange] = []
        logs: list[NormalizedLog] = []
        while pending:
            block_range = pending.pop()
            try:
                raw_logs, endpoint = await self._read_range(
                    token_address,
                    normalized_topic0,
                    block_range,
                )
            except RpcProviderLimit as exc:
                if block_range.size == 1:
                    raise DiscoveryRangeFailed(
                        f"provider cannot return complete logs for block {block_range.start}"
                    ) from exc
                left, right = block_range.split()
                pending.extend((right, left))
                continue
            except DiscoveryError:
                raise
            except Exception as exc:
                raise DiscoveryRangeFailed(
                    f"RPC failed for [{block_range.start}, {block_range.end_exclusive}); "
                    "coverage was not advanced"
                ) from exc

            if len(raw_logs) >= self._result_cap:
                # Equal-to-cap is ambiguous: many providers truncate without an
                # error exactly at the documented result limit.
                if block_range.size == 1:
                    raise DiscoveryRangeFailed(
                        f"block {block_range.start} reached the provider result cap"
                    )
                left, right = block_range.split()
                pending.extend((right, left))
                continue

            parsed = [
                _parse_log(
                    item,
                    expected_address=token_address,
                    expected_topic0=normalized_topic0,
                    requested_range=block_range,
                )
                for item in raw_logs
            ]
            logs.extend(parsed)
            completed.append(
                CompletedRange(block_range, len(parsed), endpoint.fingerprint)
            )

        completed.sort(key=lambda item: item.block_range)
        gaps = coverage_gaps(requested, (item.block_range for item in completed))
        if gaps:
            raise DiscoveryRangeFailed(f"scanner finished with uncovered ranges: {gaps!r}")
        return DiscoveryScan(
            requested_range=requested,
            completed_ranges=tuple(completed),
            logs=deduplicate_logs(logs),
        )


__all__ = [
    "BlockRange",
    "CompletedRange",
    "DiscoveryRangeFailed",
    "DiscoveryResponseError",
    "DiscoveryScan",
    "NormalizedLog",
    "StrictLogScanner",
    "contiguous_frontier",
    "coverage_gaps",
    "deduplicate_logs",
    "extract_holders",
    "merge_coverage",
]
