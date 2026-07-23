"""Persist strict event discovery without advancing coverage on any failure."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from rpc_state_indexer.config.models import TokenConfig
from rpc_state_indexer.core.discovery import (
    BlockRange,
    StrictLogScanner,
    coverage_gaps,
    extract_holders,
)
from rpc_state_indexer.evm.events import AbiRegistry
from rpc_state_indexer.rpc.client import AsyncRpcClient


class DiscoveryStore(Protocol):
    def completed_discovery_ranges(
        self, chain_id: int, token_address: str, topic0: str
    ) -> list[tuple[int, int]]: ...

    def insert_discovery_ranges(self, rows: list[dict[str, object]]) -> int: ...

    def insert_holder_observations(self, rows: list[dict[str, object]]) -> int: ...


class DiscoveryService:
    def __init__(
        self,
        *,
        chain_id: int,
        rpc: AsyncRpcClient,
        store: DiscoveryStore,
        abi_registry: AbiRegistry,
        initial_chunk_size: int,
        provider_result_cap: int,
    ) -> None:
        self.chain_id = chain_id
        self.rpc = rpc
        self.store = store
        self.abi_registry = abi_registry
        self.scanner = StrictLogScanner(
            rpc,
            initial_chunk_size=initial_chunk_size,
            provider_result_cap=provider_result_cap,
        )

    async def advance(
        self,
        token: TokenConfig,
        *,
        anchor_block: int,
        anchor_hash: str,
    ) -> None:
        required = BlockRange(token.deployment_block, anchor_block + 1)
        for event_config in token.discovery_events:
            event = self.abi_registry.validate_holder_topics(
                event_config.abi,
                event_config.event,
                event_config.holder_topics,
            )
            completed = [
                BlockRange(start, end)
                for start, end in self.store.completed_discovery_ranges(
                    self.chain_id, token.address, event.topic0
                )
            ]
            for gap in coverage_gaps(required, completed):
                await self._scan_gap(
                    token,
                    event.topic0,
                    tuple(event_config.holder_topics),
                    event_config.event,
                    gap,
                    anchor_block,
                    anchor_hash,
                    include_zero=token.zero_address_role == "holder",
                )
    async def _scan_gap(
        self,
        token: TokenConfig,
        topic0: str,
        holder_topics: tuple[int, ...],
        event_name: str,
        gap: BlockRange,
        anchor_block: int,
        anchor_hash: str,
        *,
        include_zero: bool,
    ) -> None:
        scan_id = uuid4()
        started_at = datetime.now(UTC)
        try:
            scan = await self.scanner.scan(
                token_address=token.address,
                topic0=topic0,
                start_block=gap.start,
                end_block_exclusive=gap.end_exclusive,
            )
        except Exception as exc:
            failed_at = datetime.now(UTC)
            # Preserve the underlying provider error (the DiscoveryRangeFailed wrapper text is
            # generic; the real reason — e.g. "query exceeds max results …" — is on __cause__).
            cause = exc.__cause__
            detail = (
                str(exc)
                if cause is None
                else f"{exc} | cause: {type(cause).__name__}: {cause}"
            )
            self.store.insert_discovery_ranges(
                [
                    {
                        "chain_id": self.chain_id,
                        "token_address": token.address,
                        "topic0": topic0,
                        "range_start_block": gap.start,
                        "range_end_block_exclusive": gap.end_exclusive,
                        "scan_id": scan_id,
                        "status": "failed",
                        "anchor_block": anchor_block,
                        "anchor_hash": anchor_hash,
                        "log_count": 0,
                        "holder_count": 0,
                        "attempt_count": 1,
                        "endpoint_fingerprint": "0" * 64,
                        "error_class": type(exc).__name__,
                        "error_message": detail[:4096],
                        "started_at": started_at,
                        "heartbeat_at": failed_at,
                        "finished_at": failed_at,
                    }
                ]
            )
            raise

        finished_at = datetime.now(UTC)
        range_rows: list[dict[str, object]] = []
        for completed in scan.completed_ranges:
            logs = [
                log
                for log in scan.logs
                if completed.block_range.start
                <= log.block_number
                < completed.block_range.end_exclusive
            ]
            holders = extract_holders(
                logs, holder_topics, include_zero=include_zero
            )
            range_rows.append(
                {
                    "chain_id": self.chain_id,
                    "token_address": token.address,
                    "topic0": topic0,
                    "range_start_block": completed.block_range.start,
                    "range_end_block_exclusive": completed.block_range.end_exclusive,
                    "scan_id": scan_id,
                    "status": "completed",
                    "anchor_block": anchor_block,
                    "anchor_hash": anchor_hash,
                    "log_count": completed.log_count,
                    "holder_count": len(holders),
                    "attempt_count": 1,
                    "endpoint_fingerprint": completed.endpoint_fingerprint,
                    "error_class": "",
                    "error_message": "",
                    "started_at": started_at,
                    "heartbeat_at": finished_at,
                    "finished_at": finished_at,
                }
            )

        observations: dict[str, list[int]] = defaultdict(list)
        for log in scan.logs:
            for holder in log.holders(holder_topics, include_zero=include_zero):
                observations[holder].append(log.block_number)
        holder_rows = [
            {
                "chain_id": self.chain_id,
                "token_address": token.address,
                "holder_address": holder,
                "source": "own_scan",
                "source_detail": event_name,
                "first_seen_block": min(blocks),
                "last_seen_block": max(blocks),
                "observations": len(blocks),
            }
            for holder, blocks in sorted(observations.items())
        ]

        # Persist observations before the completed coverage markers.  A crash may
        # duplicate aggregate observations on retry, but can never claim coverage
        # without all dependent rows having been accepted by ClickHouse.
        if holder_rows:
            self.store.insert_holder_observations(holder_rows)
        self.store.insert_discovery_ranges(range_rows)
