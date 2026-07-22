"""Balancer pool reserves read from the Vault, not ``balanceOf(pool)``.

Balancer custodies pool tokens in a chain-singleton Vault, so a pool's own ``balanceOf`` is
~0. Reserves come from one Vault call per pool:

- **V2** ``getPoolTokens(bytes32 poolId)`` -> ``(address[] tokens, uint256[] balances, uint256)``
- **V3** ``getPoolTokenInfo(address pool)`` -> ``(address[] tokens, TokenInfo[], uint256[]
  balancesRaw, uint256[] scaled18)`` (we take ``balancesRaw``).

The verified executor already returns raw returndata with sentinels/digest/anchor-hash
checked, so this collector reuses it and adds only the version-specific decode. Failures and
structural anomalies become errors, never zero reserves.
"""

from __future__ import annotations

from collections.abc import Callable

from rpc_state_indexer.config.models import (
    BALANCER_V2_CLASS,
    BALANCER_V3_CLASS,
    PoolConfig,
)
from rpc_state_indexer.domain import (
    BlockRef,
    IntegrityMode,
    IntegrityResult,
    ObservationStatus,
    PoolBalanceRow,
)
from rpc_state_indexer.evm.calldata import (
    get_pool_token_info_calldata,
    get_pool_tokens_calldata,
)
from rpc_state_indexer.evm.decoding import (
    BalancerDecodeError,
    decode_balancer_v2_pool_tokens,
    decode_balancer_v3_pool_token_info,
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
from .models import CollectionBatchEvidence, CollectionError, PoolCollectionResult

_Decoder = Callable[[bytes], tuple[tuple[str, int], ...]]


class BalancerPoolCollector:
    def __init__(
        self,
        executor: HistoricalCallExecutor,
        *,
        v2_vault: str | None,
        v3_vault: str | None,
    ) -> None:
        self.executor = executor
        self._v2_vault = v2_vault
        self._v3_vault = v3_vault

    def handles(self, pool: PoolConfig) -> bool:
        return pool.pool_class in {BALANCER_V2_CLASS, BALANCER_V3_CLASS}

    def vault_target(self, pool: PoolConfig) -> str:
        """The Vault address this pool's reserves are read from (for code verification)."""
        return self._plan(pool)[0]

    def _plan(self, pool: PoolConfig) -> tuple[str, bytes, str, _Decoder]:
        """Return (vault_target, calldata, call_kind, decoder) for the pool's version."""
        if pool.pool_class == BALANCER_V2_CLASS:
            if self._v2_vault is None:
                raise ValueError("balancer_v2 vault is not configured for this chain")
            if pool.pool_id is None:  # pragma: no cover - guarded by config validation
                raise ValueError("balancer_v2 pool is missing pool_id")
            return (
                self._v2_vault,
                get_pool_tokens_calldata(pool.pool_id),
                "getPoolTokens",
                decode_balancer_v2_pool_tokens,
            )
        if pool.pool_class == BALANCER_V3_CLASS:
            if self._v3_vault is None:
                raise ValueError("balancer_v3 vault is not configured for this chain")
            return (
                self._v3_vault,
                get_pool_token_info_calldata(pool.address),
                "getPoolTokenInfo",
                decode_balancer_v3_pool_token_info,
            )
        raise ValueError(f"BalancerPoolCollector cannot handle pool_class {pool.pool_class!r}")

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

        expected_tokens = sorted(asset.token for asset in pool.assets)
        if len(expected_tokens) != len(set(expected_tokens)):
            raise ValueError("pool asset list contains duplicate token addresses")

        vault, calldata, call_kind, decoder = self._plan(pool)
        key = f"balancer/{pool.address}"
        verified_batches = await self.executor.execute(
            [ContractCall(key=key, target=vault, calldata=calldata)],
            anchor,
        )

        evidence: list[CollectionBatchEvidence] = []
        found: tuple[RawCallResult, int] | None = None
        for sequence, batch in enumerate(verified_batches):
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
                if raw.key != key:
                    raise CollectionProtocolError(f"unexpected result key {raw.key!r}")
                if found is not None:
                    raise CollectionProtocolError("collector received a duplicate result")
                found = (raw, sequence)
        if found is None:
            raise CollectionProtocolError("collector received no result for the pool")
        raw, batch_sequence = found

        balances, errors = self._materialize(
            pool=pool,
            expected_tokens=expected_tokens,
            call_kind=call_kind,
            decoder=decoder,
            raw=raw,
            batch_sequence=batch_sequence,
        )

        expected_calls = len(expected_tokens)
        complete = len(balances) == expected_calls and not errors
        return PoolCollectionResult(
            pool_address=pool.address,
            integrity_mode=integrity_mode,
            expected_calls=expected_calls,
            balances=tuple(balances),
            errors=tuple(errors),
            batches=tuple(evidence),
            integrity_checks=(
                IntegrityResult(
                    passed=complete,
                    check="pool_asset_observations_complete",
                    observed=len(balances),
                    expected=expected_calls,
                ),
            ),
        )

    def _materialize(
        self,
        *,
        pool: PoolConfig,
        expected_tokens: list[str],
        call_kind: str,
        decoder: _Decoder,
        raw: RawCallResult,
        batch_sequence: int,
    ) -> tuple[list[PoolBalanceRow], list[CollectionError]]:
        def error(status: ObservationStatus, message: str) -> list[CollectionError]:
            return [
                CollectionError(
                    subject_address=pool.address,
                    call_kind=call_kind,
                    status=status,
                    batch_sequence=batch_sequence,
                    message=message,
                    rpc_code=raw.error_code,
                    return_data=raw.returndata,
                )
            ]

        if not raw.success:
            failure = classify_rpc_failure(
                RpcResponseError(raw.error_code or -1, raw.error_message or "vault call failed")
            )
            status = (
                ObservationStatus.REVERTED
                if failure.kind is FailureKind.EXECUTION_REVERT
                else ObservationStatus.RPC_ERROR
            )
            return [], error(status, raw.error_message or "vault call failed")

        try:
            pairs = decoder(raw.returndata)
        except BalancerDecodeError as exc:
            return [], error(ObservationStatus.MALFORMED_RETURN, str(exc))

        returned = dict(pairs)
        if len(returned) != len(pairs):
            return [], error(
                ObservationStatus.MALFORMED_RETURN, "vault returned a duplicate token"
            )
        # Configured assets must be a subset of the Vault's tokens. A superset (e.g. a
        # composable pool's own BPT) is ignored; a missing configured asset is a hard error.
        if not set(expected_tokens).issubset(returned):
            missing = sorted(set(expected_tokens) - set(returned))
            return [], error(
                ObservationStatus.MALFORMED_RETURN,
                f"configured assets absent from vault return: {missing}",
            )

        balances = [
            PoolBalanceRow(
                pool_address=pool.address,
                token_address=token_address,
                balance_raw=returned[token_address],
                batch_sequence=batch_sequence,
            )
            for token_address in expected_tokens
        ]
        return balances, []


__all__ = ["BalancerPoolCollector"]
