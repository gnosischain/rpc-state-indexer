from collections.abc import Sequence

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.execution.base import ContractCall, VerifiedBatchResult
from rpc_state_indexer.execution.router import HistoricalExecutorRouter


class FakeExecutor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[Sequence[ContractCall], BlockRef]] = []

    async def execute(
        self, calls: Sequence[ContractCall], anchor: BlockRef
    ) -> list[VerifiedBatchResult]:
        self.calls.append((calls, anchor))
        return []


def block(number: int) -> BlockRef:
    return BlockRef(number, "0x" + "11" * 32, "0x" + "22" * 32, 1)


def test_router_selects_legacy_before_deployment() -> None:
    legacy = FakeExecutor("legacy")
    multicall = FakeExecutor("multicall")
    router = HistoricalExecutorRouter(
        multicall_deployment_block=100,
        multicall_executor=multicall,
        legacy_executor=legacy,
    )
    assert router.for_anchor(block(99)) is legacy


def test_router_selects_multicall_at_deployment() -> None:
    legacy = FakeExecutor("legacy")
    multicall = FakeExecutor("multicall")
    router = HistoricalExecutorRouter(
        multicall_deployment_block=100,
        multicall_executor=multicall,
        legacy_executor=legacy,
    )
    assert router.for_anchor(block(100)) is multicall


async def test_router_delegates_without_changing_calls() -> None:
    legacy = FakeExecutor("legacy")
    multicall = FakeExecutor("multicall")
    router = HistoricalExecutorRouter(
        multicall_deployment_block=100,
        multicall_executor=multicall,
        legacy_executor=legacy,
    )
    calls = [ContractCall("x", "0x" + "aa" * 20, b"\x12")]
    anchor = block(101)
    await router.execute(calls, anchor)
    assert multicall.calls == [(calls, anchor)]
    assert legacy.calls == []
