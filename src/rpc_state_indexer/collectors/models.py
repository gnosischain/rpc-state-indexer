"""Immutable outputs shared by all state collectors."""

from __future__ import annotations

from dataclasses import dataclass

from rpc_state_indexer.domain import (
    BalanceRow,
    IntegrityMode,
    IntegrityResult,
    ObservationStatus,
    PoolBalanceRow,
    PoolClStateRow,
    PoolTickRow,
    ScalarRow,
)
from rpc_state_indexer.execution.base import VerificationEvidence
from rpc_state_indexer.storage.digests import (
    BalanceDigestRow,
    PoolBalanceDigestRow,
    PoolClStateDigestRow,
    PoolTickDigestRow,
    ScalarDigestRow,
    digest_cl_observations,
    digest_pool_observations,
    digest_token_observations,
)


@dataclass(frozen=True, slots=True)
class CollectionBatchEvidence:
    batch_sequence: int
    body_call_count: int
    evidence: VerificationEvidence


@dataclass(frozen=True, slots=True)
class CollectionError:
    """A terminal call or decode failure; it never carries a numeric value."""

    subject_address: str
    call_kind: str
    status: ObservationStatus
    batch_sequence: int
    message: str
    rpc_code: int | None = None
    return_data: bytes = b""


@dataclass(frozen=True, slots=True)
class TokenCollectionResult:
    token_address: str
    universe_hash: str
    integrity_mode: IntegrityMode
    expected_calls: int
    balances: tuple[BalanceRow, ...]
    scalars: tuple[ScalarRow, ...]
    errors: tuple[CollectionError, ...]
    batches: tuple[CollectionBatchEvidence, ...]
    integrity_checks: tuple[IntegrityResult, ...]

    @property
    def successful_calls(self) -> int:
        return len(self.balances) + len(self.scalars)

    @property
    def verified(self) -> bool:
        return (
            not self.errors
            and all(batch.evidence.verified for batch in self.batches)
            and all(check.passed for check in self.integrity_checks)
        )

    @property
    def result_digest(self) -> str:
        return digest_token_observations(
            (
                BalanceDigestRow(
                    holder_address=row.holder_address,
                    balance_raw=row.balance_raw,
                    scaled_balance_raw=row.scaled_balance_raw,
                    value_kind=row.value_kind,
                )
                for row in self.balances
            ),
            (
                ScalarDigestRow(
                    scalar_name=row.scalar_name,
                    scalar_raw=row.scalar_raw,
                )
                for row in self.scalars
            ),
        )


@dataclass(frozen=True, slots=True)
class PoolCollectionResult:
    pool_address: str
    integrity_mode: IntegrityMode
    expected_calls: int
    balances: tuple[PoolBalanceRow, ...]
    errors: tuple[CollectionError, ...]
    batches: tuple[CollectionBatchEvidence, ...]
    integrity_checks: tuple[IntegrityResult, ...]

    @property
    def successful_calls(self) -> int:
        return len(self.balances)

    @property
    def verified(self) -> bool:
        return (
            not self.errors
            and all(batch.evidence.verified for batch in self.batches)
            and all(check.passed for check in self.integrity_checks)
        )

    @property
    def result_digest(self) -> str:
        return digest_pool_observations(
            PoolBalanceDigestRow(
                pool_address=row.pool_address,
                token_address=row.token_address,
                balance_raw=row.balance_raw,
            )
            for row in self.balances
        )


@dataclass(frozen=True, slots=True)
class PoolClCollectionResult:
    """Concentrated-liquidity primitives for one pool at an anchor.

    ``ticks`` is empty when the pool was below the active-liquidity threshold (state-only)
    or genuinely has no initialized ticks. The ΣliquidityNet invariants live in
    ``integrity_checks`` and gate publication.
    """

    pool_address: str
    integrity_mode: IntegrityMode
    expected_calls: int
    state: PoolClStateRow
    ticks: tuple[PoolTickRow, ...]
    errors: tuple[CollectionError, ...]
    batches: tuple[CollectionBatchEvidence, ...]
    integrity_checks: tuple[IntegrityResult, ...]

    @property
    def successful_calls(self) -> int:
        return 1 + len(self.ticks)

    @property
    def verified(self) -> bool:
        return (
            not self.errors
            and all(batch.evidence.verified for batch in self.batches)
            and all(check.passed for check in self.integrity_checks)
        )

    @property
    def result_digest(self) -> str:
        return digest_cl_observations(
            PoolClStateDigestRow(
                pool_address=self.state.pool_address,
                sqrt_price_x96=self.state.sqrt_price_x96,
                current_tick=self.state.current_tick,
                liquidity=self.state.liquidity,
                fee_growth_global_0_x128=self.state.fee_growth_global_0_x128,
                fee_growth_global_1_x128=self.state.fee_growth_global_1_x128,
                tick_spacing=self.state.tick_spacing,
                fee=self.state.fee,
                tick_count=self.state.tick_count,
            ),
            (
                PoolTickDigestRow(
                    pool_address=row.pool_address,
                    tick=row.tick,
                    liquidity_gross=row.liquidity_gross,
                    liquidity_net=row.liquidity_net,
                    fee_growth_outside_0_x128=row.fee_growth_outside_0_x128,
                    fee_growth_outside_1_x128=row.fee_growth_outside_1_x128,
                )
                for row in self.ticks
            ),
        )


__all__ = [
    "CollectionBatchEvidence",
    "CollectionError",
    "PoolClCollectionResult",
    "PoolCollectionResult",
    "TokenCollectionResult",
]
