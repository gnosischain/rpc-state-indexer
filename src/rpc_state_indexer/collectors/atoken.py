"""Aave/Spark aToken scaled-state collection and exact ray reconstruction."""

from __future__ import annotations

from rpc_state_indexer.config.models import TokenConfig
from rpc_state_indexer.domain import (
    BalanceRow,
    BlockRef,
    FrozenUniverse,
    IntegrityMode,
    IntegrityResult,
    ObservationStatus,
    ScalarRow,
)
from rpc_state_indexer.evm.calldata import address_word, function_selector
from rpc_state_indexer.execution.base import ContractCall, HistoricalCallExecutor

from .common import UIntCallSpec, completeness_check, execute_uint_calls
from .erc20 import _assert_universe, scalar_calldata
from .models import CollectionError, TokenCollectionResult

RAY = 10**27
HALF_RAY = 5 * 10**26
UINT256_MAX = (1 << 256) - 1


def ray_mul_half_up(value: int, ray_factor: int) -> int:
    """Aave's exact positive ``rayMul``: nearest integer with ties rounded up."""

    if isinstance(value, bool) or isinstance(ray_factor, bool):
        raise TypeError("rayMul operands must be integers")
    if not isinstance(value, int) or not isinstance(ray_factor, int):
        raise TypeError("rayMul operands must be integers")
    if not 0 <= value <= UINT256_MAX or not 0 <= ray_factor <= UINT256_MAX:
        raise ValueError("rayMul operand is outside uint256")
    output = (value * ray_factor + HALF_RAY) // RAY
    if output > UINT256_MAX:
        raise OverflowError("rayMul result is outside uint256")
    return output


class ATokenCollector:
    def __init__(self, executor: HistoricalCallExecutor) -> None:
        self.executor = executor

    async def collect(
        self,
        *,
        token: TokenConfig,
        universe: FrozenUniverse,
        anchor: BlockRef,
        integrity_mode: IntegrityMode,
    ) -> TokenCollectionResult:
        if not token.is_atoken:
            raise ValueError("ATokenCollector requires an aToken config")
        if token.balance_function != "scaledBalanceOf":
            raise ValueError("aToken collector requires scaledBalanceOf")
        if token.index_source is None:  # pragma: no cover - Pydantic enforces this
            raise ValueError("aToken config has no index source")
        if integrity_mode not in {
            IntegrityMode.SCALED_FULL_SUPPLY,
            IntegrityMode.SCOPED,
        }:
            raise ValueError(f"unsupported aToken integrity mode: {integrity_mode}")
        if anchor.number < token.deployment_block:
            raise ValueError("token is not deployed at the requested anchor")
        _assert_universe(universe)

        specs: list[UIntCallSpec] = []
        balance_keys: list[str] = []
        scaled_selector = function_selector("scaledBalanceOf(address)")
        for holder in universe.addresses:
            key = f"scaledBalanceOf/{holder}"
            balance_keys.append(key)
            specs.append(
                UIntCallSpec(
                    call=ContractCall(
                        key=key,
                        target=token.address,
                        calldata=scaled_selector + address_word(holder),
                    ),
                    subject_address=holder,
                    call_kind="scaledBalanceOf",
                )
            )

        scalar_keys: list[tuple[str, str]] = []
        seen_scalars: set[str] = set()
        for name in token.supply_functions:
            if name in seen_scalars:
                raise ValueError(f"duplicate supply function: {name}")
            seen_scalars.add(name)
            key = f"scalar/{name}"
            scalar_keys.append((name, key))
            specs.append(
                UIntCallSpec(
                    call=ContractCall(
                        key=key,
                        target=token.address,
                        calldata=scalar_calldata(name),
                    ),
                    subject_address=token.address,
                    call_kind=name,
                )
            )

        index = token.index_source
        index_key = f"scalar/{index.output_name}"
        specs.append(
            UIntCallSpec(
                call=ContractCall(
                    key=index_key,
                    target=index.contract,
                    calldata=(
                        function_selector("getReserveNormalizedIncome(address)")
                        + address_word(index.argument)
                    ),
                ),
                subject_address=index.contract,
                call_kind=index.function,
            )
        )

        decoded = await execute_uint_calls(self.executor, specs, anchor)
        errors: list[CollectionError] = []
        scalars: list[ScalarRow] = []
        scalar_values: dict[str, int] = {}
        scaled_values: dict[str, tuple[int, int]] = {}

        for holder, key in zip(universe.addresses, balance_keys, strict=True):
            call = decoded.calls[key]
            if not call.observation.ok:
                errors.append(call.as_error())
                continue
            value = call.observation.value
            if value is None:  # pragma: no cover - guarded by UIntObservation
                raise AssertionError("successful uint256 observation has no value")
            scaled_values[holder] = (value, call.batch_sequence)

        for name, key in (*scalar_keys, (index.output_name, index_key)):
            call = decoded.calls[key]
            if not call.observation.ok:
                errors.append(call.as_error())
                continue
            value = call.observation.value
            if value is None:  # pragma: no cover - guarded by UIntObservation
                raise AssertionError("successful uint256 observation has no value")
            scalar_values[name] = value
            scalars.append(
                ScalarRow(
                    scalar_name=name,
                    scalar_raw=value,
                    batch_sequence=call.batch_sequence,
                )
            )

        balances: list[BalanceRow] = []
        liquidity_index = scalar_values.get(index.output_name)
        if liquidity_index is None:
            for holder, (_, batch_sequence) in scaled_values.items():
                errors.append(
                    CollectionError(
                        subject_address=holder,
                        call_kind="ray_reconstruct",
                        status=ObservationStatus.RPC_ERROR,
                        batch_sequence=batch_sequence,
                        message=f"{index.output_name} is unavailable",
                    )
                )
        else:
            for holder, (scaled, batch_sequence) in scaled_values.items():
                try:
                    reconstructed = ray_mul_half_up(scaled, liquidity_index)
                except (OverflowError, TypeError, ValueError) as exc:
                    errors.append(
                        CollectionError(
                            subject_address=holder,
                            call_kind="ray_reconstruct",
                            status=ObservationStatus.MALFORMED_RETURN,
                            batch_sequence=batch_sequence,
                            message=str(exc),
                        )
                    )
                    continue
                balances.append(
                    BalanceRow(
                        holder_address=holder,
                        balance_raw=reconstructed,
                        scaled_balance_raw=scaled,
                        value_kind="ray_reconstructed",
                        batch_sequence=batch_sequence,
                    )
                )

        calls_complete, successful = completeness_check(
            expected_calls=len(specs),
            decoded=decoded,
        )
        complete = calls_complete and len(balances) == universe.size and not errors
        checks: list[IntegrityResult] = [
            IntegrityResult(
                passed=complete,
                check="observations_complete",
                observed=successful,
                expected=len(specs),
            )
        ]
        if integrity_mode is IntegrityMode.SCALED_FULL_SUPPLY:
            observed_scaled_supply = sum(value for value, _ in scaled_values.values())
            expected_scaled_supply = scalar_values.get("scaledTotalSupply")
            checks.append(
                IntegrityResult(
                    passed=(
                        complete
                        and expected_scaled_supply is not None
                        and observed_scaled_supply == expected_scaled_supply
                    ),
                    check="scaled_holder_sum_equals_scaled_total_supply",
                    observed=observed_scaled_supply,
                    expected=expected_scaled_supply,
                )
            )
        else:
            checks.append(IntegrityResult.complete("scoped_coverage_no_supply_invariant"))

        return TokenCollectionResult(
            token_address=token.address,
            universe_hash=universe.universe_hash,
            integrity_mode=integrity_mode,
            expected_calls=len(specs),
            balances=tuple(balances),
            scalars=tuple(scalars),
            errors=tuple(errors),
            batches=decoded.batches,
            integrity_checks=tuple(checks),
        )


__all__ = ["ATokenCollector", "HALF_RAY", "RAY", "ray_mul_half_up"]
