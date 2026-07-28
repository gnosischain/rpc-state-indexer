"""Resolve ERC-20 display metadata (symbol/name/decimals) at an immutable block.

Metadata is read once per token, batched through the same verified executor as balances, and
persisted as an observation keyed by (chain_id, token_address) — never folded back into the
catalog, so a token's config_hash cannot move when its metadata is later resolved.

Resolution never blocks admission. A token whose calls revert still has an exact balance;
only its label degrades, and the failure is recorded rather than papered over.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.evm.calldata import function_selector
from rpc_state_indexer.evm.metadata_decoding import (
    decode_decimals_return,
    decode_text_return,
)
from rpc_state_indexer.execution.base import ContractCall, HistoricalCallExecutor

SYMBOL_SELECTOR = function_selector("symbol()")
NAME_SELECTOR = function_selector("name()")
DECIMALS_SELECTOR = function_selector("decimals()")

STATUS_RESOLVED = "resolved"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    chain_id: int
    token_address: str
    symbol: str | None
    name: str | None
    decimals: int | None
    resolution_status: str
    symbol_encoding: str
    name_encoding: str
    anchor_block: int
    anchor_hash: str
    error_class: str = ""
    error_message: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "token_address": self.token_address,
            "symbol": self.symbol,
            "name": self.name,
            "decimals": self.decimals,
            "resolution_status": self.resolution_status,
            "symbol_encoding": self.symbol_encoding,
            "name_encoding": self.name_encoding,
            "anchor_block": self.anchor_block,
            "anchor_hash": self.anchor_hash,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "observed_at": datetime.now(UTC),
        }


def _status(symbol: str | None, name: str | None, decimals: int | None) -> str:
    observed = sum(value is not None for value in (symbol, name, decimals))
    if observed == 3:
        return STATUS_RESOLVED
    return STATUS_PARTIAL if observed else STATUS_FAILED


class TokenMetadataCollector:
    def __init__(self, executor: HistoricalCallExecutor, *, chain_id: int) -> None:
        self.executor = executor
        self.chain_id = chain_id

    def _calls(self, addresses: tuple[str, ...]) -> list[ContractCall]:
        calls: list[ContractCall] = []
        for address in addresses:
            # allow_failure: a token that does not implement these is common and must not
            # fail the batch for every other token in it.
            calls.append(
                ContractCall(f"{address}:symbol", address, SYMBOL_SELECTOR, True)
            )
            calls.append(ContractCall(f"{address}:name", address, NAME_SELECTOR, True))
            calls.append(
                ContractCall(f"{address}:decimals", address, DECIMALS_SELECTOR, True)
            )
        return calls

    async def resolve(
        self,
        addresses: tuple[str, ...],
        anchor: BlockRef,
    ) -> tuple[TokenMetadata, ...]:
        """Read metadata for every address at the anchor. Never raises for a bad token."""

        if not addresses:
            return ()
        ordered = tuple(sorted(set(addresses)))
        results: dict[str, Any] = {}
        for batch in await self.executor.execute(self._calls(ordered), anchor):
            for result in batch.results:
                results[result.key] = result

        resolved: list[TokenMetadata] = []
        for address in ordered:
            symbol_result = results.get(f"{address}:symbol")
            name_result = results.get(f"{address}:name")
            decimals_result = results.get(f"{address}:decimals")
            if symbol_result is None or name_result is None or decimals_result is None:
                # The executor did not return one of the calls: record the absence rather
                # than inventing a value, so the next pass retries this token.
                resolved.append(
                    TokenMetadata(
                        chain_id=self.chain_id,
                        token_address=address,
                        symbol=None,
                        name=None,
                        decimals=None,
                        resolution_status=STATUS_FAILED,
                        symbol_encoding="absent",
                        name_encoding="absent",
                        anchor_block=anchor.number,
                        anchor_hash=anchor.block_hash,
                        error_class="MissingCallResult",
                        error_message="executor returned no result for one or more calls",
                    )
                )
                continue

            symbol = decode_text_return(symbol_result.success, symbol_result.returndata)
            name = decode_text_return(name_result.success, name_result.returndata)
            decimals = decode_decimals_return(
                decimals_result.success, decimals_result.returndata
            )
            status = _status(symbol.value, name.value, decimals)
            detail = ""
            if status != STATUS_RESOLVED:
                detail = "; ".join(
                    part
                    for part in (
                        f"symbol: {symbol.detail}" if not symbol.resolved else "",
                        f"name: {name.detail}" if not name.resolved else "",
                        "decimals: not a canonical uint8 word" if decimals is None else "",
                    )
                    if part
                )
            resolved.append(
                TokenMetadata(
                    chain_id=self.chain_id,
                    token_address=address,
                    symbol=symbol.value,
                    name=name.value,
                    decimals=decimals,
                    resolution_status=status,
                    symbol_encoding=symbol.encoding,
                    name_encoding=name.encoding,
                    anchor_block=anchor.number,
                    anchor_hash=anchor.block_hash,
                    error_class="" if status == STATUS_RESOLVED else "UnresolvedMetadata",
                    error_message=detail[:4096],
                )
            )
        return tuple(resolved)


__all__ = [
    "DECIMALS_SELECTOR",
    "NAME_SELECTOR",
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_RESOLVED",
    "SYMBOL_SELECTOR",
    "TokenMetadata",
    "TokenMetadataCollector",
]
