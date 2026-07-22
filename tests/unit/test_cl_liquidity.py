"""ClLiquidityCollector multi-round behaviour with a fake verified executor.

Uses a large on-chain ``tickSpacing`` so the bitmap scan range stays small, and places a
single position (lower +net / upper -net) that satisfies the ΣliquidityNet invariants.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from rpc_state_indexer.collectors.cl_liquidity import ClLiquidityCollector
from rpc_state_indexer.collectors.common import CollectionProtocolError
from rpc_state_indexer.config.models import PoolAssetConfig, PoolConfig
from rpc_state_indexer.domain import BlockRef, ExecutorKind, IntegrityMode
from rpc_state_indexer.evm.calldata import TICK_BITMAP_SELECTOR, TICK_TABLE_SELECTOR
from rpc_state_indexer.execution.base import (
    ContractCall,
    RawCallResult,
    VerificationEvidence,
    VerifiedBatchResult,
    digest_raw_results,
)

POOL = "0x" + "b0" * 20
TOKEN_A = "0x" + "11" * 20
TOKEN_B = "0x" + "22" * 20
ANCHOR = BlockRef(100, "0x" + "ab" * 32, "0x" + "cd" * 32, 1234)
SPACING = 4096  # keeps the scan range to a couple of bitmap words
L = 10**18
ZERO_WORD = b"\x00" * 32


def _w(value: int) -> bytes:
    return (value & (2**256 - 1)).to_bytes(32, "big")


def slot0_bytes(sqrt_price: int, tick: int) -> bytes:
    return _w(sqrt_price) + _w(tick) + _w(0) + _w(0) + _w(0) + _w(0) + _w(1)


def globalstate_bytes(sqrt_price: int, tick: int, fee: int) -> bytes:
    return _w(sqrt_price) + _w(tick) + _w(fee) + _w(0) + _w(0) + _w(0) + _w(1)


def ticks_bytes(gross: int, net: int, *, initialized: bool = True) -> bytes:
    return (
        _w(gross) + _w(net) + _w(0) + _w(0) + _w(0) + _w(0) + _w(0)
        + _w(1 if initialized else 0)
    )


def bitmap_bytes(*bits: int) -> bytes:
    value = 0
    for bit in bits:
        value |= 1 << bit
    return _w(value)


class FakeExecutor:
    def __init__(self, responses: Mapping[str, bytes | tuple[int, str]]) -> None:
        self.responses = dict(responses)
        self.rounds: list[tuple[ContractCall, ...]] = []

    async def execute(
        self, calls: Sequence[ContractCall], anchor: BlockRef
    ) -> list[VerifiedBatchResult]:
        self.rounds.append(tuple(calls))
        results = []
        for call in calls:
            if call.key in self.responses:
                resp = self.responses[call.key]
                if isinstance(resp, tuple):
                    results.append(RawCallResult(call.key, False, b"", resp[0], resp[1]))
                else:
                    results.append(RawCallResult(call.key, True, resp))
            elif call.key.startswith("bm/"):
                results.append(RawCallResult(call.key, True, ZERO_WORD))
            else:  # pragma: no cover - a missing key is a test bug
                raise KeyError(call.key)
        body = tuple(results)
        return [
            VerifiedBatchResult(
                body,
                VerificationEvidence(
                    executor_kind=ExecutorKind.MULTICALL3,
                    block_reference_kind="eip1898",
                    anchor_hash=anchor.block_hash,
                    provider_groups=("fake",),
                    result_digest=digest_raw_results(body),
                    verified=True,
                ),
            )
        ]


def uni_pool() -> PoolConfig:
    return PoolConfig(
        address=POOL, name="A-B uni", pool_class="uniswap_v3", deployment_block=1,
        assets=[PoolAssetConfig(token=TOKEN_A), PoolAssetConfig(token=TOKEN_B)],
    )


def alg_pool() -> PoolConfig:
    return PoolConfig(
        address=POOL, name="A-B swapr", pool_class="swapr_v3_algebra", deployment_block=1,
        assets=[PoolAssetConfig(token=TOKEN_A), PoolAssetConfig(token=TOKEN_B)],
    )


def uni_state(tick: int = 0, liquidity: int = L) -> dict[str, bytes]:
    return {
        "slot0": slot0_bytes(1 << 96, tick),
        "liquidity": _w(liquidity),
        "tickSpacing": _w(SPACING),
        "fee": _w(500),
        "feeGrowth0": _w(0),
        "feeGrowth1": _w(0),
    }


# Uniswap compressed convention: tick -4096 -> compressed -1 -> word -1 bit 255;
#                                tick  4096 -> compressed  1 -> word  0 bit 1.
UNI_POSITION = {
    "bm/-1": bitmap_bytes(255),
    "bm/0": bitmap_bytes(1),
    "tk/-4096": ticks_bytes(L, L),
    "tk/4096": ticks_bytes(L, -L),
}


@pytest.mark.asyncio
async def test_uniswap_happy_path_reconciles_and_publishes() -> None:
    exe = FakeExecutor({**uni_state(), **UNI_POSITION})
    result = await ClLiquidityCollector(exe).collect(pool=uni_pool(), anchor=ANCHOR)

    assert result.verified
    assert result.state.current_tick == 0
    assert result.state.liquidity == L
    assert result.state.tick_spacing == SPACING
    assert result.state.tick_count == 2
    nets = {row.tick: row.liquidity_net for row in result.ticks}
    assert nets == {-4096: L, 4096: -L}
    checks = {c.check: c.passed for c in result.integrity_checks}
    assert checks["cl_liquidity_net_sum_zero"] and checks["cl_active_liquidity_reconciles"]
    # bitmap round used the compressed-key getter
    assert exe.rounds[1][0].calldata[:4] == TICK_BITMAP_SELECTOR


@pytest.mark.asyncio
async def test_algebra_happy_path_uses_raw_bitmap_key() -> None:
    # Algebra raw convention: tick -4096 -> word -16 bit 0; tick 4096 -> word 16 bit 0.
    responses = {
        "globalState": globalstate_bytes(1 << 96, 0, 2000),
        "liquidity": _w(L),
        "tickSpacing": _w(SPACING),
        "feeGrowth0": _w(0),
        "feeGrowth1": _w(0),
        "bm/-16": bitmap_bytes(0),
        "bm/16": bitmap_bytes(0),
        "tk/-4096": ticks_bytes(L, L),
        "tk/4096": ticks_bytes(L, -L),
    }
    exe = FakeExecutor(responses)
    result = await ClLiquidityCollector(exe).collect(pool=alg_pool(), anchor=ANCHOR)

    assert result.verified
    assert result.state.fee == 2000
    assert {row.tick for row in result.ticks} == {-4096, 4096}
    assert exe.rounds[1][0].calldata[:4] == TICK_TABLE_SELECTOR


@pytest.mark.asyncio
async def test_below_threshold_is_state_only() -> None:
    exe = FakeExecutor({**uni_state(liquidity=5), **UNI_POSITION})
    coll = ClLiquidityCollector(exe, min_active_liquidity=1000)
    result = await coll.collect(pool=uni_pool(), anchor=ANCHOR)

    assert result.verified
    assert result.ticks == ()
    assert result.state.tick_count == 0
    assert len(exe.rounds) == 1  # no bitmap or tick rounds
    assert any(c.check == "cl_below_active_threshold" for c in result.integrity_checks)


@pytest.mark.asyncio
async def test_sum_net_not_zero_blocks_publication() -> None:
    broken = {**UNI_POSITION, "tk/4096": ticks_bytes(L, -(L // 2))}  # nets don't cancel
    exe = FakeExecutor({**uni_state(), **broken})
    result = await ClLiquidityCollector(exe).collect(pool=uni_pool(), anchor=ANCHOR)

    assert not result.verified
    checks = {c.check: c.passed for c in result.integrity_checks}
    assert checks["cl_liquidity_net_sum_zero"] is False


@pytest.mark.asyncio
async def test_active_liquidity_mismatch_blocks_publication() -> None:
    exe = FakeExecutor({**uni_state(liquidity=L + 1), **UNI_POSITION})  # net says L, state L+1
    result = await ClLiquidityCollector(exe).collect(pool=uni_pool(), anchor=ANCHOR)

    assert not result.verified
    checks = {c.check: c.passed for c in result.integrity_checks}
    assert checks["cl_active_liquidity_reconciles"] is False


@pytest.mark.asyncio
async def test_reverted_tick_becomes_error_not_zero() -> None:
    responses = {**uni_state(), **UNI_POSITION, "tk/4096": (3, "execution reverted")}
    exe = FakeExecutor(responses)
    result = await ClLiquidityCollector(exe).collect(pool=uni_pool(), anchor=ANCHOR)

    assert not result.verified
    assert result.errors
    assert result.errors[0].call_kind == "ticks"
    # the surviving tick was still recorded; the failure is an error, never a zero row
    assert all(row.tick != 4096 for row in result.ticks)


@pytest.mark.asyncio
async def test_bitmap_flag_without_initialized_struct_is_error() -> None:
    responses = {
        **uni_state(), **UNI_POSITION,
        "tk/4096": ticks_bytes(0, 0, initialized=False),  # bitmap said set, struct disagrees
    }
    exe = FakeExecutor(responses)
    result = await ClLiquidityCollector(exe).collect(pool=uni_pool(), anchor=ANCHOR)

    assert not result.verified
    assert any(e.call_kind == "ticks" for e in result.errors)


@pytest.mark.asyncio
async def test_reverted_state_read_raises() -> None:
    responses = {**uni_state(), **UNI_POSITION, "slot0": (3, "execution reverted")}
    exe = FakeExecutor(responses)
    with pytest.raises(ValueError, match="CL state read"):
        await ClLiquidityCollector(exe).collect(pool=uni_pool(), anchor=ANCHOR)


@pytest.mark.asyncio
async def test_wrong_integrity_mode_rejected() -> None:
    exe = FakeExecutor({**uni_state(), **UNI_POSITION})
    with pytest.raises(ValueError, match="cl_liquidity"):
        await ClLiquidityCollector(exe).collect(
            pool=uni_pool(), anchor=ANCHOR, integrity_mode=IntegrityMode.POOL_ASSETS
        )


@pytest.mark.asyncio
async def test_unverified_batch_is_protocol_error() -> None:
    class BadExecutor(FakeExecutor):
        async def execute(self, calls, anchor):  # type: ignore[no-untyped-def]
            batches = await super().execute(calls, anchor)
            good = batches[0]
            bad = VerificationEvidence(
                executor_kind=good.evidence.executor_kind,
                block_reference_kind=good.evidence.block_reference_kind,
                anchor_hash=good.evidence.anchor_hash,
                provider_groups=good.evidence.provider_groups,
                result_digest=good.evidence.result_digest,
                verified=False,
            )
            return [VerifiedBatchResult(good.results, bad)]

    exe = BadExecutor({**uni_state(), **UNI_POSITION})
    with pytest.raises(CollectionProtocolError):
        await ClLiquidityCollector(exe).collect(pool=uni_pool(), anchor=ANCHOR)
