"""Persist wallet-interaction sweeps without advancing coverage on any failure.

A sweep scans address-less ``eth_getLogs`` filters that put the universe's wallets in one
indexed topic position at a time. Coverage is tracked per (wallet, topic_position), so a
newly added wallet backfills alone while existing wallets keep their coverage. Wallets
whose gaps coincide are batched into one OR-list filter per scan.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from rpc_state_indexer.config.models import SweepConfig
from rpc_state_indexer.core.discovery import (
    BlockRange,
    DiscoveryResponseError,
    LogFilter,
    StrictLogScanner,
    coverage_gaps,
)
from rpc_state_indexer.rpc.client import AsyncRpcClient


class SweepStore(Protocol):
    def completed_sweep_ranges(
        self, chain_id: int, wallet_address: str, topic_position: int
    ) -> list[tuple[int, int]]: ...

    def insert_sweep_ranges(self, rows: list[dict[str, object]]) -> int: ...

    def insert_wallet_interaction_logs(self, rows: list[dict[str, object]]) -> int: ...


def wallet_topic(address: str) -> str:
    """Left-pad a 20-byte address to the 32-byte indexed-topic word."""

    return "0x" + "0" * 24 + address[2:]


def scan_windows(block_range: BlockRange, size: int) -> Iterator[BlockRange]:
    """Split a range into commit-sized windows, in ascending order."""

    if size < 1:
        raise ValueError("checkpoint window must be positive")
    cursor = block_range.start
    while cursor < block_range.end_exclusive:
        end = min(cursor + size, block_range.end_exclusive)
        yield BlockRange(cursor, end)
        cursor = end


class SweepService:
    def __init__(
        self,
        *,
        chain_id: int,
        rpc: AsyncRpcClient,
        store: SweepStore,
        initial_chunk_size: int,
        provider_result_cap: int,
        on_progress: Callable[..., None] | None = None,
    ) -> None:
        self.chain_id = chain_id
        self.store = store
        self.on_progress = on_progress
        self.scanner = StrictLogScanner(
            rpc,
            initial_chunk_size=initial_chunk_size,
            provider_result_cap=provider_result_cap,
        )

    def _progress(self, event: str, **fields: object) -> None:
        if self.on_progress is not None:
            self.on_progress(event, **fields)

    async def advance(
        self,
        sweep: SweepConfig,
        wallets: tuple[str, ...],
        *,
        anchor_block: int,
        anchor_hash: str,
    ) -> None:
        if not wallets:
            raise ValueError(f"sweep {sweep.name} resolved an empty wallet universe")
        required = BlockRange(sweep.start_block, anchor_block + 1)
        for position in sorted(sweep.topic_positions):
            gap_groups: dict[tuple[BlockRange, ...], list[str]] = defaultdict(list)
            for wallet in sorted(set(wallets)):
                completed = [
                    BlockRange(start, end)
                    for start, end in self.store.completed_sweep_ranges(
                        self.chain_id, wallet, position
                    )
                ]
                gaps = coverage_gaps(required, completed)
                if gaps:
                    gap_groups[gaps].append(wallet)
            for gaps, group in sorted(gap_groups.items()):
                windows = [
                    window
                    for gap in gaps
                    for window in scan_windows(gap, sweep.checkpoint_blocks)
                ]
                self._progress(
                    "sweep_position_start",
                    sweep=sweep.name,
                    topic_position=position,
                    wallets=len(group),
                    from_block=windows[0].start,
                    to_block=windows[-1].end_exclusive - 1,
                    windows=len(windows),
                )
                for index, window in enumerate(windows, start=1):
                    logs = await self._scan_gap(
                        position, tuple(group), window, anchor_block, anchor_hash
                    )
                    # Each window is committed before the next starts, so this line also
                    # marks durable, resumable coverage — not just progress.
                    self._progress(
                        "sweep_window_indexed",
                        sweep=sweep.name,
                        topic_position=position,
                        from_block=window.start,
                        to_block=window.end_exclusive - 1,
                        window=index,
                        windows=len(windows),
                        logs=logs,
                    )

    async def _scan_gap(
        self,
        position: int,
        wallets: tuple[str, ...],
        gap: BlockRange,
        anchor_block: int,
        anchor_hash: str,
    ) -> int:
        scan_id = uuid4()
        started_at = datetime.now(UTC)
        wallet_by_topic = {wallet_topic(wallet): wallet for wallet in wallets}
        constraints: list[tuple[str, ...] | None] = [None] * position
        constraints.append(tuple(sorted(wallet_by_topic)))
        log_filter = LogFilter(address=None, topics=tuple(constraints))
        try:
            scan = await self.scanner.scan_filter(
                log_filter=log_filter,
                start_block=gap.start,
                end_block_exclusive=gap.end_exclusive,
            )
        except Exception as exc:
            failed_at = datetime.now(UTC)
            cause = exc.__cause__
            detail = (
                str(exc)
                if cause is None
                else f"{exc} | cause: {type(cause).__name__}: {cause}"
            )
            self.store.insert_sweep_ranges(
                [
                    {
                        "chain_id": self.chain_id,
                        "wallet_address": wallet,
                        "topic_position": position,
                        "range_start_block": gap.start,
                        "range_end_block_exclusive": gap.end_exclusive,
                        "scan_id": scan_id,
                        "status": "failed",
                        "anchor_block": anchor_block,
                        "anchor_hash": anchor_hash,
                        "log_count": 0,
                        "attempt_count": 1,
                        "endpoint_fingerprint": "0" * 64,
                        "error_class": type(exc).__name__,
                        "error_message": detail[:4096],
                        "started_at": started_at,
                        "heartbeat_at": failed_at,
                        "finished_at": failed_at,
                    }
                    for wallet in wallets
                ]
            )
            raise

        finished_at = datetime.now(UTC)
        log_rows: list[dict[str, object]] = []
        counts: dict[tuple[str, int], int] = defaultdict(int)
        for log in scan.logs:
            wallet = wallet_by_topic.get(log.topics[position])
            if wallet is None:
                # scan_filter already enforced topic membership; a miss here means the
                # padded word decoded to no requested wallet — provider garbage.
                raise DiscoveryResponseError(
                    "sweep log topic does not match a requested wallet"
                )
            counts[(wallet, log.block_number)] += 1
            log_rows.append(
                {
                    "chain_id": self.chain_id,
                    "wallet_address": wallet,
                    "topic_position": position,
                    "contract_address": log.address,
                    "topic0": log.topic0,
                    "topic_count": len(log.topics),
                    "block_number": log.block_number,
                    "block_hash": log.block_hash,
                    "transaction_hash": log.transaction_hash,
                    "log_index": log.log_index,
                    "topics": list(log.topics),
                    "observed_at": finished_at,
                }
            )

        range_rows: list[dict[str, object]] = []
        for completed in scan.completed_ranges:
            for wallet in wallets:
                log_count = sum(
                    count
                    for (counted_wallet, block), count in counts.items()
                    if counted_wallet == wallet
                    and completed.block_range.start
                    <= block
                    < completed.block_range.end_exclusive
                )
                range_rows.append(
                    {
                        "chain_id": self.chain_id,
                        "wallet_address": wallet,
                        "topic_position": position,
                        "range_start_block": completed.block_range.start,
                        "range_end_block_exclusive": completed.block_range.end_exclusive,
                        "scan_id": scan_id,
                        "status": "completed",
                        "anchor_block": anchor_block,
                        "anchor_hash": anchor_hash,
                        "log_count": log_count,
                        "attempt_count": 1,
                        "endpoint_fingerprint": completed.endpoint_fingerprint,
                        "error_class": "",
                        "error_message": "",
                        "started_at": started_at,
                        "heartbeat_at": finished_at,
                        "finished_at": finished_at,
                    }
                )

        # Persist evidence before the completed coverage markers. A crash may duplicate
        # log rows on retry (deduplicated by ReplacingMergeTree), but can never claim
        # coverage without all dependent rows having been accepted by ClickHouse.
        if log_rows:
            self.store.insert_wallet_interaction_logs(log_rows)
        self.store.insert_sweep_ranges(range_rows)
        return len(log_rows)
