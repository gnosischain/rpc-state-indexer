from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pytest

from rpc_state_indexer.collectors.atoken import RAY, ATokenCollector, ray_mul_half_up
from rpc_state_indexer.collectors.erc20 import Erc20Collector
from rpc_state_indexer.collectors.pools import PoolReserveCollector
from rpc_state_indexer.config.models import (
    EventConfig,
    IndexSourceConfig,
    PoolAssetConfig,
    PoolConfig,
    TokenConfig,
)
from rpc_state_indexer.domain import (
    BlockRef,
    ExecutorKind,
    FrozenUniverse,
    IntegrityMode,
    ObservationStatus,
)
from rpc_state_indexer.execution.base import (
    ContractCall,
    RawCallResult,
    VerificationEvidence,
    VerifiedBatchResult,
    digest_raw_results,
)
from rpc_state_indexer.storage.digests import digest_universe

TOKEN = "0x" + "99" * 20
POOL = "0x" + "88" * 20
INDEX_SOURCE = "0x" + "77" * 20
RESERVE = "0x" + "66" * 20
A = "0x" + "11" * 20
B = "0x" + "22" * 20
C = "0x" + "33" * 20
ANCHOR = BlockRef(100, "0x" + "ab" * 32, "0x" + "cd" * 32, 1234)


@dataclass(frozen=True)
class FailedCall:
    code: int = -32000
    message: str = "execution reverted"


class FakeExecutor:
    def __init__(self, responses: Mapping[str, int | bytes | FailedCall]) -> None:
        self.responses = responses
        self.seen_calls: tuple[ContractCall, ...] = ()

    async def execute(
        self,
        calls: Sequence[ContractCall],
        anchor: BlockRef,
    ) -> list[VerifiedBatchResult]:
        self.seen_calls = tuple(calls)
        results: list[RawCallResult] = []
        for call in calls:
            response = self.responses[call.key]
            if isinstance(response, FailedCall):
                results.append(
                    RawCallResult(
                        call.key,
                        False,
                        b"",
                        response.code,
                        response.message,
                    )
                )
            else:
                raw = response.to_bytes(32, "big") if isinstance(response, int) else response
                results.append(RawCallResult(call.key, True, raw))
        body = tuple(results)
        return [
            VerifiedBatchResult(
                body,
                VerificationEvidence(
                    executor_kind=ExecutorKind.MULTICALL3,
                    block_reference_kind="eip1898",
                    anchor_hash=anchor.block_hash,
                    provider_groups=("fake-a",),
                    result_digest=digest_raw_results(body),
                    verified=True,
                ),
            )
        ]


def token_config() -> TokenConfig:
    return TokenConfig(
        address=TOKEN,
        symbol="TEST",
        decimals=18,
        token_class="standard_erc20",
        deployment_block=1,
        balance_function="balanceOf",
        supply_functions=["totalSupply"],
        discovery_events=[
            EventConfig(abi="erc20", event="Transfer", holder_topics=[1, 2])
        ],
    )


def atoken_config() -> TokenConfig:
    return TokenConfig(
        address=TOKEN,
        symbol="aTEST",
        decimals=18,
        token_class="aave_v3_atoken",
        deployment_block=1,
        balance_function="scaledBalanceOf",
        supply_functions=["totalSupply", "scaledTotalSupply"],
        index_source=IndexSourceConfig(
            contract=INDEX_SOURCE,
            function="getReserveNormalizedIncome",
            argument=RESERVE,
        ),
        discovery_events=[
            EventConfig(abi="erc20", event="Transfer", holder_topics=[1, 2])
        ],
    )


def universe(*addresses: str) -> FrozenUniverse:
    sources = {address: ("test",) for address in sorted(addresses)}
    return FrozenUniverse(tuple(sorted(addresses)), sources, digest_universe(sources))


@pytest.mark.asyncio
async def test_observed_zero_is_retained_while_failure_is_not_a_zero() -> None:
    executor = FakeExecutor(
        {
            f"balanceOf/{A}": 0,
            f"balanceOf/{B}": FailedCall(),
            "scalar/totalSupply": 0,
        }
    )

    result = await Erc20Collector(executor).collect(
        token=token_config(),
        universe=universe(A, B),
        anchor=ANCHOR,
        integrity_mode=IntegrityMode.SCOPED,
    )

    assert [(row.holder_address, row.balance_raw) for row in result.balances] == [(A, 0)]
    assert [(row.scalar_name, row.scalar_raw) for row in result.scalars] == [
        ("totalSupply", 0)
    ]
    assert len(result.errors) == 1
    assert result.errors[0].subject_address == B
    assert result.errors[0].status is ObservationStatus.REVERTED
    assert result.verified is False


@pytest.mark.asyncio
async def test_empty_success_return_is_an_error_not_zero() -> None:
    executor = FakeExecutor(
        {
            f"balanceOf/{A}": b"",
            "scalar/totalSupply": 0,
        }
    )

    result = await Erc20Collector(executor).collect(
        token=token_config(),
        universe=universe(A),
        anchor=ANCHOR,
        integrity_mode=IntegrityMode.SCOPED,
    )

    assert result.balances == ()
    assert result.errors[0].status is ObservationStatus.EMPTY_RETURN
    assert result.errors[0].return_data == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(("supply", "expected"), [(3, True), (4, False)])
async def test_full_erc20_supply_is_exact(supply: int, expected: bool) -> None:
    executor = FakeExecutor(
        {
            f"balanceOf/{A}": 1,
            f"balanceOf/{B}": 2,
            "scalar/totalSupply": supply,
        }
    )

    result = await Erc20Collector(executor).collect(
        token=token_config(),
        universe=universe(A, B),
        anchor=ANCHOR,
        integrity_mode=IntegrityMode.FULL_SUPPLY,
    )

    equality = result.integrity_checks[1]
    assert equality.observed == 3
    assert equality.expected == supply
    assert equality.passed is expected
    assert result.verified is expected


@pytest.mark.parametrize(
    ("value", "factor", "expected"),
    [
        (0, RAY, 0),
        (7, RAY, 7),
        (1, RAY // 2, 1),
        (1, RAY // 2 - 1, 0),
        (3, 2 * RAY, 6),
    ],
)
def test_ray_mul_is_exact_half_up(value: int, factor: int, expected: int) -> None:
    assert ray_mul_half_up(value, factor) == expected


@pytest.mark.asyncio
async def test_atoken_reconstructs_and_checks_scaled_supply_exactly() -> None:
    executor = FakeExecutor(
        {
            f"scaledBalanceOf/{A}": 1,
            f"scaledBalanceOf/{B}": 2,
            "scalar/totalSupply": 6,
            "scalar/scaledTotalSupply": 3,
            "scalar/liquidity_index_ray": 2 * RAY,
        }
    )

    result = await ATokenCollector(executor).collect(
        token=atoken_config(),
        universe=universe(A, B),
        anchor=ANCHOR,
        integrity_mode=IntegrityMode.SCALED_FULL_SUPPLY,
    )

    assert [row.scaled_balance_raw for row in result.balances] == [1, 2]
    assert [row.balance_raw for row in result.balances] == [2, 4]
    assert result.integrity_checks[1].observed == 3
    assert result.integrity_checks[1].expected == 3
    assert result.verified is True


@pytest.mark.asyncio
async def test_atoken_index_failure_never_turns_scaled_zero_into_balance_zero() -> None:
    executor = FakeExecutor(
        {
            f"scaledBalanceOf/{A}": 0,
            "scalar/totalSupply": 0,
            "scalar/scaledTotalSupply": 0,
            "scalar/liquidity_index_ray": FailedCall(message="archive state unavailable"),
        }
    )

    result = await ATokenCollector(executor).collect(
        token=atoken_config(),
        universe=universe(A),
        anchor=ANCHOR,
        integrity_mode=IntegrityMode.SCALED_FULL_SUPPLY,
    )

    assert result.balances == ()
    assert {error.call_kind for error in result.errors} == {
        "getReserveNormalizedIncome",
        "ray_reconstruct",
    }
    assert result.verified is False


@pytest.mark.asyncio
async def test_pool_balances_keep_zero_and_isolate_an_asset_failure() -> None:
    token_two = "0x" + "55" * 20
    pool = PoolConfig(
        address=POOL,
        name="test pool",
        pool_class="test",
        deployment_block=1,
        assets=[PoolAssetConfig(token=TOKEN), PoolAssetConfig(token=token_two)],
    )
    executor = FakeExecutor(
        {
            f"reserve/{POOL}/{token_two}": FailedCall(),
            f"reserve/{POOL}/{TOKEN}": 0,
        }
    )

    result = await PoolReserveCollector(executor).collect(pool=pool, anchor=ANCHOR)

    assert len(result.balances) == 1
    assert result.balances[0].balance_raw == 0
    assert result.errors[0].subject_address == token_two
    assert result.verified is False
