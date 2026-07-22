"""Pool reserves observed as each asset token's ``balanceOf(pool)``."""

from __future__ import annotations

from rpc_state_indexer.config.models import PoolConfig
from rpc_state_indexer.domain import (
    BlockRef,
    IntegrityMode,
    IntegrityResult,
    PoolBalanceRow,
)
from rpc_state_indexer.evm.calldata import balance_of_calldata
from rpc_state_indexer.execution.base import ContractCall, HistoricalCallExecutor

from .common import UIntCallSpec, completeness_check, execute_uint_calls
from .models import CollectionError, PoolCollectionResult


class PoolReserveCollector:
    def __init__(self, executor: HistoricalCallExecutor) -> None:
        self.executor = executor

    async def collect(
        self,
        *,
        pool: PoolConfig,
        anchor: BlockRef,
        integrity_mode: IntegrityMode = IntegrityMode.POOL_ASSETS,
    ) -> PoolCollectionResult:
        if integrity_mode is not IntegrityMode.POOL_ASSETS:
            raise ValueError("pool reserve collection requires pool_assets integrity")
        if anchor.number < pool.deployment_block:
            raise ValueError("pool is not deployed at the requested anchor")

        assets = sorted(asset.token for asset in pool.assets)
        if len(assets) != len(set(assets)):
            raise ValueError("pool asset list contains duplicate token addresses")
        specs = [
            UIntCallSpec(
                call=ContractCall(
                    key=f"reserve/{pool.address}/{token_address}",
                    target=token_address,
                    calldata=balance_of_calldata(pool.address),
                ),
                subject_address=token_address,
                call_kind="balanceOf(pool)",
            )
            for token_address in assets
        ]
        decoded = await execute_uint_calls(self.executor, specs, anchor)

        balances: list[PoolBalanceRow] = []
        errors: list[CollectionError] = []
        for token_address, spec in zip(assets, specs, strict=True):
            call = decoded.calls[spec.call.key]
            if not call.observation.ok:
                errors.append(call.as_error())
                continue
            value = call.observation.value
            if value is None:  # pragma: no cover - guarded by UIntObservation
                raise AssertionError("successful uint256 observation has no value")
            balances.append(
                PoolBalanceRow(
                    pool_address=pool.address,
                    token_address=token_address,
                    balance_raw=value,
                    batch_sequence=call.batch_sequence,
                )
            )

        complete, successful = completeness_check(
            expected_calls=len(specs),
            decoded=decoded,
        )
        return PoolCollectionResult(
            pool_address=pool.address,
            integrity_mode=integrity_mode,
            expected_calls=len(specs),
            balances=tuple(balances),
            errors=tuple(errors),
            batches=decoded.batches,
            integrity_checks=(
                IntegrityResult(
                    passed=complete,
                    check="pool_asset_observations_complete",
                    observed=successful,
                    expected=len(specs),
                ),
            ),
        )


__all__ = ["PoolReserveCollector"]
