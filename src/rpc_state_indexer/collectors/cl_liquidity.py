"""Concentrated-liquidity primitive ingestion (Uniswap V3 + Swapr/Algebra V3).

Lands raw on-chain CL primitives pinned to an anchor via the verified executor — pool state
plus every initialized tick — and self-validates against the ΣliquidityNet invariants. No
reconstruction math here: the liquidity profile is derived later in the compute layer.

Layouts and the two divergent bitmap conventions are documented in [[cl-liquidity-profile]]
and [[cl-bitmap-convention-differs]]. Failures and structural anomalies become errors or
failing integrity checks; nothing is ever coerced to a zero or an empty tick set.
"""

from __future__ import annotations

from collections.abc import Sequence

from rpc_state_indexer.config.models import (
    CL_POOL_CLASSES,
    UNISWAP_V3_CLASS,
    PoolConfig,
)
from rpc_state_indexer.domain import (
    BlockRef,
    IntegrityMode,
    IntegrityResult,
    ObservationStatus,
    PoolClStateRow,
    PoolTickRow,
)
from rpc_state_indexer.evm.calldata import (
    FEE_GROWTH_GLOBAL_0_SELECTOR,
    FEE_GROWTH_GLOBAL_1_SELECTOR,
    FEE_SELECTOR,
    GLOBAL_STATE_SELECTOR,
    LIQUIDITY_SELECTOR,
    MAX_TICK,
    MIN_TICK,
    SLOT0_SELECTOR,
    TICK_SPACING_SELECTOR,
    TOTAL_FEE_GROWTH_0_SELECTOR,
    TOTAL_FEE_GROWTH_1_SELECTOR,
    tick_bitmap_calldata,
    tick_table_calldata,
    ticks_calldata,
)
from rpc_state_indexer.evm.decoding import (
    ClDecodeError,
    decode_bitmap_word,
    decode_global_state,
    decode_int_return,
    decode_slot0,
    decode_ticks,
    decode_uint_return,
)
from rpc_state_indexer.execution.base import (
    ContractCall,
    HistoricalCallExecutor,
    RawCallResult,
    digest_raw_results,
)
from rpc_state_indexer.rpc.classification import FailureKind, classify_rpc_failure
from rpc_state_indexer.rpc.errors import RpcResponseError

from .common import CollectionProtocolError
from .models import (
    CollectionBatchEvidence,
    CollectionError,
    PoolClCollectionResult,
)

_UINT128 = 128
_UINT256 = 256
_INT24 = 24


class ClLiquidityCollector:
    """Multi-round CL primitive collector, dispatched by ``pool_class``.

    ``min_active_liquidity`` gates the expensive tick enumeration: a pool whose active
    ``liquidity()`` is below the threshold is published state-only (no ticks).
    """

    def __init__(
        self,
        executor: HistoricalCallExecutor,
        *,
        min_active_liquidity: int = 0,
    ) -> None:
        self.executor = executor
        self.min_active_liquidity = max(0, min_active_liquidity)

    def handles(self, pool: PoolConfig) -> bool:
        return pool.pool_class in CL_POOL_CLASSES

    async def collect(
        self,
        *,
        pool: PoolConfig,
        anchor: BlockRef,
        integrity_mode: IntegrityMode = IntegrityMode.CL_LIQUIDITY,
    ) -> PoolClCollectionResult:
        if integrity_mode is not IntegrityMode.CL_LIQUIDITY:
            raise ValueError("CL collection requires cl_liquidity integrity")
        if pool.pool_class not in CL_POOL_CLASSES:
            raise ValueError(
                f"ClLiquidityCollector cannot handle pool_class {pool.pool_class!r}"
            )
        if anchor.number < pool.deployment_block:
            raise ValueError("pool is not deployed at the requested anchor")

        evidence: list[CollectionBatchEvidence] = []
        errors: list[CollectionError] = []
        sequence = 0

        # --- round 1: pool state (mandatory; a pool that cannot report state is a hard fail)
        state_calls = self._state_calls(pool)
        state_raw, sequence = await self._run_round(
            pool, anchor, state_calls, evidence, sequence
        )
        state, state_seq = self._decode_state(pool, state_raw)
        expected_calls = len(state_calls)

        checks = [IntegrityResult(True, "cl_state_observed")]
        ticks: list[PoolTickRow] = []

        if state.liquidity < self.min_active_liquidity:
            # Below threshold: publish state only, skip the tick sweep.
            checks.append(
                IntegrityResult(
                    True,
                    "cl_below_active_threshold",
                    observed=state.liquidity,
                    expected=self.min_active_liquidity,
                )
            )
        else:
            # --- round 2: tick discovery via the bitmap
            bitmap_calls = self._bitmap_calls(pool, state.tick_spacing)
            expected_calls += len(bitmap_calls)
            bitmap_raw, sequence = await self._run_round(
                pool, anchor, bitmap_calls, evidence, sequence
            )
            discovered, discovery_errors = self._discover_ticks(
                pool, state.tick_spacing, bitmap_raw
            )
            errors.extend(discovery_errors)

            # --- round 3: read every initialized tick
            tick_calls = [
                ContractCall(key=f"tk/{tick}", target=pool.address, calldata=ticks_calldata(tick))
                for tick in discovered
            ]
            expected_calls += len(tick_calls)
            if tick_calls:
                tick_raw, sequence = await self._run_round(
                    pool, anchor, tick_calls, evidence, sequence
                )
                ticks, tick_errors = self._materialize_ticks(pool, discovered, tick_raw)
                errors.extend(tick_errors)

            checks.extend(self._reconcile(state, ticks))

        state = PoolClStateRow(
            pool_address=state.pool_address,
            pool_class=state.pool_class,
            sqrt_price_x96=state.sqrt_price_x96,
            current_tick=state.current_tick,
            liquidity=state.liquidity,
            fee_growth_global_0_x128=state.fee_growth_global_0_x128,
            fee_growth_global_1_x128=state.fee_growth_global_1_x128,
            tick_spacing=state.tick_spacing,
            fee=state.fee,
            tick_count=len(ticks),
            batch_sequence=state_seq,
        )
        return PoolClCollectionResult(
            pool_address=pool.address,
            integrity_mode=integrity_mode,
            expected_calls=expected_calls,
            state=state,
            ticks=tuple(ticks),
            errors=tuple(errors),
            batches=tuple(evidence),
            integrity_checks=tuple(checks),
        )

    # -- round execution ---------------------------------------------------------------

    async def _run_round(
        self,
        pool: PoolConfig,
        anchor: BlockRef,
        calls: Sequence[ContractCall],
        evidence: list[CollectionBatchEvidence],
        start_sequence: int,
    ) -> tuple[dict[str, tuple[RawCallResult, int]], int]:
        """Execute one verified round; return {key: (raw, batch_sequence)} and the next seq.

        Reuses the executor's sentinel/anchor/digest contract exactly like the other
        collectors; a violation is a hard ``CollectionProtocolError``.
        """
        expected = {call.key for call in calls}
        if len(expected) != len(calls):
            raise ValueError("CL round has duplicate call keys")
        verified_batches = await self.executor.execute(list(calls), anchor)
        results: dict[str, tuple[RawCallResult, int]] = {}
        sequence = start_sequence
        for batch in verified_batches:
            if not batch.evidence.verified:
                raise CollectionProtocolError(f"batch {sequence} is not verified")
            if batch.evidence.anchor_hash.lower() != anchor.block_hash.lower():
                raise CollectionProtocolError(f"batch {sequence} anchor hash mismatch")
            if batch.evidence.result_digest != digest_raw_results(batch.results):
                raise CollectionProtocolError(f"batch {sequence} result digest mismatch")
            evidence.append(
                CollectionBatchEvidence(
                    batch_sequence=sequence,
                    body_call_count=len(batch.results),
                    evidence=batch.evidence,
                )
            )
            for raw in batch.results:
                if raw.key not in expected:
                    raise CollectionProtocolError(f"unexpected result key {raw.key!r}")
                if raw.key in results:
                    raise CollectionProtocolError(f"duplicate result for {raw.key!r}")
                results[raw.key] = (raw, sequence)
            sequence += 1
        missing = sorted(expected - set(results))
        if missing:
            raise CollectionProtocolError(f"round is missing result keys: {missing}")
        return results, sequence

    # -- state round -------------------------------------------------------------------

    def _state_calls(self, pool: PoolConfig) -> list[ContractCall]:
        def call(key: str, selector: bytes) -> ContractCall:
            return ContractCall(key=key, target=pool.address, calldata=selector)

        common = [
            call("liquidity", LIQUIDITY_SELECTOR),
            call("tickSpacing", TICK_SPACING_SELECTOR),
        ]
        if pool.pool_class == UNISWAP_V3_CLASS:
            return [
                call("slot0", SLOT0_SELECTOR),
                call("fee", FEE_SELECTOR),
                call("feeGrowth0", FEE_GROWTH_GLOBAL_0_SELECTOR),
                call("feeGrowth1", FEE_GROWTH_GLOBAL_1_SELECTOR),
                *common,
            ]
        return [  # Algebra: fee comes from globalState, fee growth from totalFeeGrowth*
            call("globalState", GLOBAL_STATE_SELECTOR),
            call("feeGrowth0", TOTAL_FEE_GROWTH_0_SELECTOR),
            call("feeGrowth1", TOTAL_FEE_GROWTH_1_SELECTOR),
            *common,
        ]

    def _decode_state(
        self, pool: PoolConfig, raw: dict[str, tuple[RawCallResult, int]]
    ) -> tuple[PoolClStateRow, int]:
        def value(key: str) -> RawCallResult:
            result, _ = raw[key]
            if not result.success:
                raise ValueError(
                    f"CL state read {key!r} failed for {pool.address}: "
                    f"{result.error_message or 'reverted'}"
                )
            return result

        try:
            if pool.pool_class == UNISWAP_V3_CLASS:
                slot0 = decode_slot0(value("slot0").returndata)
                sqrt_price, current_tick = slot0.sqrt_price_x96, slot0.tick
                fee = decode_uint_return(value("fee").returndata, bits=_INT24)
                state_seq = raw["slot0"][1]
            else:
                gs = decode_global_state(value("globalState").returndata)
                sqrt_price, current_tick, fee = gs.sqrt_price_x96, gs.tick, gs.fee
                state_seq = raw["globalState"][1]
            liquidity = decode_uint_return(value("liquidity").returndata, bits=_UINT128)
            tick_spacing = decode_int_return(value("tickSpacing").returndata, bits=_INT24)
            fee_growth_0 = decode_uint_return(value("feeGrowth0").returndata, bits=_UINT256)
            fee_growth_1 = decode_uint_return(value("feeGrowth1").returndata, bits=_UINT256)
        except ClDecodeError as exc:
            raise ValueError(f"CL state decode failed for {pool.address}: {exc}") from exc

        if tick_spacing <= 0:
            raise ValueError(f"CL pool {pool.address} reported non-positive tickSpacing")

        state = PoolClStateRow(
            pool_address=pool.address,
            pool_class=pool.pool_class,
            sqrt_price_x96=sqrt_price,
            current_tick=current_tick,
            liquidity=liquidity,
            fee_growth_global_0_x128=fee_growth_0,
            fee_growth_global_1_x128=fee_growth_1,
            tick_spacing=tick_spacing,
            fee=fee,
            tick_count=0,
            batch_sequence=state_seq,
        )
        return state, state_seq

    # -- tick discovery ----------------------------------------------------------------

    def _bitmap_calls(self, pool: PoolConfig, tick_spacing: int) -> list[ContractCall]:
        if pool.pool_class == UNISWAP_V3_CLASS:
            word_min = (MIN_TICK // tick_spacing) >> 8
            word_max = (MAX_TICK // tick_spacing) >> 8
            builder = tick_bitmap_calldata
        else:  # Algebra tickTable is keyed by the raw tick, not the compressed tick
            word_min = MIN_TICK >> 8
            word_max = MAX_TICK >> 8
            builder = tick_table_calldata
        return [
            ContractCall(key=f"bm/{wp}", target=pool.address, calldata=builder(wp))
            for wp in range(word_min, word_max + 1)
        ]

    def _discover_ticks(
        self,
        pool: PoolConfig,
        tick_spacing: int,
        raw: dict[str, tuple[RawCallResult, int]],
    ) -> tuple[list[int], list[CollectionError]]:
        compressed = pool.pool_class == UNISWAP_V3_CLASS
        found: set[int] = set()
        errors: list[CollectionError] = []
        for key, (result, seq) in raw.items():
            word_position = int(key.split("/", 1)[1])
            if not result.success:
                errors.append(
                    self._error(pool, "tickBitmap", result, seq, f"word {word_position}")
                )
                continue
            try:
                bits = decode_bitmap_word(result.returndata)
            except ClDecodeError as exc:
                errors.append(
                    self._error(
                        pool, "tickBitmap", result, seq, str(exc),
                        status=ObservationStatus.MALFORMED_RETURN,
                    )
                )
                continue
            for bit in bits:
                index = word_position * 256 + bit
                tick = index * tick_spacing if compressed else index
                if MIN_TICK <= tick <= MAX_TICK:
                    found.add(tick)
        return sorted(found), errors

    def _materialize_ticks(
        self,
        pool: PoolConfig,
        discovered: Sequence[int],
        raw: dict[str, tuple[RawCallResult, int]],
    ) -> tuple[list[PoolTickRow], list[CollectionError]]:
        ticks: list[PoolTickRow] = []
        errors: list[CollectionError] = []
        for tick in discovered:
            result, seq = raw[f"tk/{tick}"]
            if not result.success:
                errors.append(self._error(pool, "ticks", result, seq, f"tick {tick}"))
                continue
            try:
                decoded = decode_ticks(result.returndata)
            except ClDecodeError as exc:
                errors.append(
                    self._error(
                        pool, "ticks", result, seq, f"tick {tick}: {exc}",
                        status=ObservationStatus.MALFORMED_RETURN,
                    )
                )
                continue
            # The bitmap advertised this tick as initialized; the struct must agree.
            if not decoded.initialized or decoded.liquidity_gross == 0:
                errors.append(
                    self._error(
                        pool, "ticks", result, seq,
                        f"tick {tick} flagged in bitmap but not initialized",
                        status=ObservationStatus.MALFORMED_RETURN,
                    )
                )
                continue
            ticks.append(
                PoolTickRow(
                    pool_address=pool.address,
                    tick=tick,
                    liquidity_gross=decoded.liquidity_gross,
                    liquidity_net=decoded.liquidity_net,
                    fee_growth_outside_0_x128=decoded.fee_growth_outside_0_x128,
                    fee_growth_outside_1_x128=decoded.fee_growth_outside_1_x128,
                    batch_sequence=seq,
                )
            )
        return ticks, errors

    def _reconcile(
        self, state: PoolClStateRow, ticks: Sequence[PoolTickRow]
    ) -> list[IntegrityResult]:
        net_sum = sum(row.liquidity_net for row in ticks)
        active = sum(row.liquidity_net for row in ticks if row.tick <= state.current_tick)
        return [
            IntegrityResult(
                net_sum == 0, "cl_liquidity_net_sum_zero", observed=net_sum, expected=0
            ),
            IntegrityResult(
                active == state.liquidity,
                "cl_active_liquidity_reconciles",
                observed=active,
                expected=state.liquidity,
            ),
        ]

    # -- errors ------------------------------------------------------------------------

    def _error(
        self,
        pool: PoolConfig,
        call_kind: str,
        result: RawCallResult,
        sequence: int,
        detail: str,
        *,
        status: ObservationStatus | None = None,
    ) -> CollectionError:
        if status is None:
            failure = classify_rpc_failure(
                RpcResponseError(result.error_code or -1, result.error_message or "call failed")
            )
            status = (
                ObservationStatus.REVERTED
                if failure.kind is FailureKind.EXECUTION_REVERT
                else ObservationStatus.RPC_ERROR
            )
        return CollectionError(
            subject_address=pool.address,
            call_kind=call_kind,
            status=status,
            batch_sequence=sequence,
            message=f"{detail}: {result.error_message}" if result.error_message else detail,
            rpc_code=result.error_code,
            return_data=result.returndata,
        )


__all__ = ["ClLiquidityCollector"]
