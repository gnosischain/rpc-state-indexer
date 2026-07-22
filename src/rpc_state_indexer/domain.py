from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID


class ObservationStatus(StrEnum):
    OK = "ok"
    REVERTED = "reverted"
    EMPTY_RETURN = "empty_return"
    MALFORMED_RETURN = "malformed_return"
    NO_CODE = "no_code"
    RPC_ERROR = "rpc_error"
    ANCHOR_MISMATCH = "anchor_mismatch"


class IntegrityMode(StrEnum):
    FULL_SUPPLY = "full_supply"
    SCALED_FULL_SUPPLY = "scaled_full_supply"
    SCOPED = "scoped"
    POOL_ASSETS = "pool_assets"
    CL_LIQUIDITY = "cl_liquidity"


class ExecutorKind(StrEnum):
    MULTICALL3 = "multicall3"
    LEGACY_RPC_BATCH = "legacy_rpc_batch"


@dataclass(frozen=True, slots=True)
class BlockRef:
    number: int
    block_hash: str
    parent_hash: str
    timestamp: int


@dataclass(frozen=True, slots=True)
class UIntObservation:
    status: ObservationStatus
    value: int | None
    raw: bytes = b""
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status is ObservationStatus.OK:
            if self.value is None or self.value < 0:
                raise ValueError("successful UInt observation requires a value")
        elif self.value is not None:
            raise ValueError("failed observation must not contain a value")

    @property
    def ok(self) -> bool:
        return self.status is ObservationStatus.OK


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: UUID
    chain_id: int
    job_name: str
    target_kind: str
    target_address: str
    snapshot_date: date
    anchor: BlockRef
    config_hash: str
    integrity_mode: IntegrityMode
    executor_kind: ExecutorKind


@dataclass(frozen=True, slots=True)
class BalanceRow:
    holder_address: str
    balance_raw: int
    scaled_balance_raw: int | None = None
    value_kind: str = "direct"
    batch_sequence: int = 0


@dataclass(frozen=True, slots=True)
class ScalarRow:
    scalar_name: str
    scalar_raw: int
    batch_sequence: int = 0


@dataclass(frozen=True, slots=True)
class PoolBalanceRow:
    pool_address: str
    token_address: str
    balance_raw: int
    batch_sequence: int = 0


@dataclass(frozen=True, slots=True)
class PoolClStateRow:
    """One concentrated-liquidity pool's state at an anchor (Uniswap V3 / Algebra)."""

    pool_address: str
    pool_class: str
    sqrt_price_x96: int
    current_tick: int
    liquidity: int
    fee_growth_global_0_x128: int
    fee_growth_global_1_x128: int
    tick_spacing: int
    fee: int
    tick_count: int
    batch_sequence: int = 0


@dataclass(frozen=True, slots=True)
class PoolTickRow:
    """One initialized tick. ``liquidity_net`` is signed (int128)."""

    pool_address: str
    tick: int
    liquidity_gross: int
    liquidity_net: int
    fee_growth_outside_0_x128: int
    fee_growth_outside_1_x128: int
    batch_sequence: int = 0


@dataclass(frozen=True, slots=True)
class FrozenUniverse:
    addresses: tuple[str, ...]
    sources: Mapping[str, tuple[str, ...]]
    universe_hash: str

    @property
    def size(self) -> int:
        return len(self.addresses)


@dataclass(frozen=True, slots=True)
class IntegrityResult:
    passed: bool
    check: str
    observed: int | None = None
    expected: int | None = None

    @classmethod
    def exact(cls, check: str, observed: int, expected: int) -> IntegrityResult:
        return cls(observed == expected, check, observed, expected)

    @classmethod
    def complete(cls, check: str) -> IntegrityResult:
        return cls(True, check)
