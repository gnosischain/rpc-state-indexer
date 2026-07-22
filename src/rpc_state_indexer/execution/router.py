from __future__ import annotations

from collections.abc import Sequence

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.execution.base import (
    ContractCall,
    HistoricalCallExecutor,
    VerifiedBatchResult,
)


class HistoricalExecutorRouter:
    def __init__(
        self,
        *,
        multicall_deployment_block: int,
        multicall_executor: HistoricalCallExecutor,
        legacy_executor: HistoricalCallExecutor,
    ) -> None:
        if multicall_deployment_block < 0:
            raise ValueError("deployment block must not be negative")
        self.multicall_deployment_block = multicall_deployment_block
        self.multicall_executor = multicall_executor
        self.legacy_executor = legacy_executor

    def for_anchor(self, anchor: BlockRef) -> HistoricalCallExecutor:
        if anchor.number >= self.multicall_deployment_block:
            return self.multicall_executor
        return self.legacy_executor

    async def execute(
        self,
        calls: Sequence[ContractCall],
        anchor: BlockRef,
    ) -> list[VerifiedBatchResult]:
        return await self.for_anchor(anchor).execute(calls, anchor)

