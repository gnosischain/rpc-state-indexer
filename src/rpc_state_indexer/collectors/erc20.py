"""ERC-20 balance and supply collection at one immutable block."""

from __future__ import annotations

from rpc_state_indexer.config.models import TokenConfig
from rpc_state_indexer.domain import (
    BalanceRow,
    BlockRef,
    FrozenUniverse,
    IntegrityMode,
    IntegrityResult,
    ScalarRow,
)
from rpc_state_indexer.evm.calldata import (
    TOTAL_SUPPLY_SELECTOR,
    balance_of_calldata,
    function_selector,
)
from rpc_state_indexer.execution.base import ContractCall, HistoricalCallExecutor

from .common import UIntCallSpec, completeness_check, execute_uint_calls
from .models import CollectionError, TokenCollectionResult

_SCALAR_CALLDATA = {
    "totalSupply": TOTAL_SUPPLY_SELECTOR,
    "scaledTotalSupply": function_selector("scaledTotalSupply()"),
}


def scalar_calldata(function_name: str) -> bytes:
    try:
        return _SCALAR_CALLDATA[function_name]
    except KeyError as exc:
        raise ValueError(f"unsupported uint256 scalar function: {function_name}") from exc


def _assert_universe(universe: FrozenUniverse) -> None:
    if universe.addresses != tuple(sorted(set(universe.addresses))):
        raise ValueError("frozen universe addresses must be unique and sorted")
    if set(universe.sources) != set(universe.addresses):
        raise ValueError("frozen universe provenance must cover every address exactly")


class Erc20Collector:
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
        if token.is_atoken:
            raise ValueError("aTokens must use ATokenCollector")
        if token.balance_function != "balanceOf":
            raise ValueError("ERC-20 collector requires balanceOf")
        if integrity_mode not in {IntegrityMode.FULL_SUPPLY, IntegrityMode.SCOPED}:
            raise ValueError(f"unsupported ERC-20 integrity mode: {integrity_mode}")
        if anchor.number < token.deployment_block:
            raise ValueError("token is not deployed at the requested anchor")
        _assert_universe(universe)

        specs: list[UIntCallSpec] = []
        balance_keys: list[str] = []
        for holder in universe.addresses:
            key = f"balanceOf/{holder}"
            balance_keys.append(key)
            specs.append(
                UIntCallSpec(
                    call=ContractCall(
                        key=key,
                        target=token.address,
                        calldata=balance_of_calldata(holder),
                    ),
                    subject_address=holder,
                    call_kind="balanceOf",
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

        decoded = await execute_uint_calls(self.executor, specs, anchor)
        balances: list[BalanceRow] = []
        scalars: list[ScalarRow] = []
        errors: list[CollectionError] = []

        for holder, key in zip(universe.addresses, balance_keys, strict=True):
            call = decoded.calls[key]
            if not call.observation.ok:
                errors.append(call.as_error())
                continue
            # The explicit None check is correctness-critical: an observed zero is a row.
            value = call.observation.value
            if value is None:  # pragma: no cover - guarded by UIntObservation
                raise AssertionError("successful uint256 observation has no value")
            balances.append(
                BalanceRow(
                    holder_address=holder,
                    balance_raw=value,
                    value_kind="direct",
                    batch_sequence=call.batch_sequence,
                )
            )

        scalar_values: dict[str, int] = {}
        for name, key in scalar_keys:
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

        complete, successful = completeness_check(
            expected_calls=len(specs),
            decoded=decoded,
        )
        checks: list[IntegrityResult] = [
            IntegrityResult(
                passed=complete,
                check="observations_complete",
                observed=successful,
                expected=len(specs),
            )
        ]
        if integrity_mode is IntegrityMode.FULL_SUPPLY:
            observed_sum = sum(row.balance_raw for row in balances)
            expected_supply = scalar_values.get("totalSupply")
            checks.append(
                IntegrityResult(
                    passed=(
                        complete
                        and expected_supply is not None
                        and observed_sum == expected_supply
                    ),
                    check="holder_sum_equals_total_supply",
                    observed=observed_sum,
                    expected=expected_supply,
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


__all__ = ["Erc20Collector", "scalar_calldata"]
