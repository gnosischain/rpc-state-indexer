"""Canonical SHA-256 digests used by the publication protocol."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_UINT256_MAX = (1 << 256) - 1
_INT256_MIN = -(1 << 255)
_INT256_MAX = (1 << 255) - 1


@dataclass(frozen=True, slots=True)
class BalanceDigestRow:
    holder_address: str
    balance_raw: int
    scaled_balance_raw: int | None = None
    value_kind: str = "direct"


@dataclass(frozen=True, slots=True)
class ScalarDigestRow:
    scalar_name: str
    scalar_raw: int


@dataclass(frozen=True, slots=True)
class PoolBalanceDigestRow:
    pool_address: str
    token_address: str
    balance_raw: int


@dataclass(frozen=True, slots=True)
class PoolClStateDigestRow:
    pool_address: str
    sqrt_price_x96: int
    current_tick: int
    liquidity: int
    fee_growth_global_0_x128: int
    fee_growth_global_1_x128: int
    tick_spacing: int
    fee: int
    tick_count: int


@dataclass(frozen=True, slots=True)
class PoolTickDigestRow:
    pool_address: str
    tick: int
    liquidity_gross: int
    liquidity_net: int
    fee_growth_outside_0_x128: int
    fee_growth_outside_1_x128: int


def _address_bytes(value: str) -> bytes:
    if not _ADDRESS_RE.fullmatch(value):
        raise ValueError(f"address must be normalized lowercase 0x hex: {value!r}")
    return bytes.fromhex(value[2:])


def _uint256_bytes(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("UInt256 digest values must be integers")
    if not 0 <= value <= _UINT256_MAX:
        raise ValueError("value is outside UInt256")
    return value.to_bytes(32, byteorder="big")


def _int256_bytes(value: int) -> bytes:
    """Signed two's-complement encoding for CL fields (tick, liquidityNet) that are
    genuinely signed. ``_uint256_bytes`` rejects negatives, so those must never use it."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Int256 digest values must be integers")
    if not _INT256_MIN <= value <= _INT256_MAX:
        raise ValueError("value is outside Int256")
    return value.to_bytes(32, byteorder="big", signed=True)


def _text_bytes(value: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > 0xFFFFFFFF:
        raise ValueError("digest text field is too large")
    return len(encoded).to_bytes(4, "big") + encoded


def digest_universe(
    members: Mapping[str, Iterable[str]] | Iterable[tuple[str, Iterable[str]]],
) -> str:
    """Hash a frozen universe independently of input or source ordering."""

    items = members.items() if isinstance(members, Mapping) else members
    canonical: list[tuple[bytes, tuple[str, ...]]] = []
    seen: set[str] = set()

    for address, sources in items:
        if address in seen:
            raise ValueError(f"duplicate universe member: {address}")
        seen.add(address)
        canonical.append((_address_bytes(address), tuple(sorted(set(sources)))))

    digest = hashlib.sha256()
    digest.update(b"rpc-state-indexer/universe/v1\x00")

    for address, sources in sorted(canonical, key=lambda item: item[0]):
        digest.update(b"U")
        digest.update(address)
        digest.update(len(sources).to_bytes(4, "big"))
        for source in sources:
            digest.update(_text_bytes(source))

    return digest.hexdigest()


def digest_token_observations(
    balances: Iterable[BalanceDigestRow],
    scalars: Iterable[ScalarDigestRow],
) -> str:
    """Hash token observations with stable ordering and exact integer encoding."""

    balance_rows = sorted(
        balances,
        key=lambda row: (
            _address_bytes(row.holder_address),
            row.value_kind,
            row.balance_raw,
            -1 if row.scaled_balance_raw is None else row.scaled_balance_raw,
        ),
    )
    scalar_rows = sorted(
        scalars,
        key=lambda row: (row.scalar_name, row.scalar_raw),
    )

    digest = hashlib.sha256()
    digest.update(b"rpc-state-indexer/token-observations/v1\x00")

    for balance_row in balance_rows:
        digest.update(b"B")
        digest.update(_address_bytes(balance_row.holder_address))
        digest.update(_text_bytes(balance_row.value_kind))
        digest.update(_uint256_bytes(balance_row.balance_raw))
        if balance_row.scaled_balance_raw is None:
            digest.update(b"\x00")
        else:
            digest.update(b"\x01")
            digest.update(_uint256_bytes(balance_row.scaled_balance_raw))

    for scalar_row in scalar_rows:
        digest.update(b"S")
        digest.update(_text_bytes(scalar_row.scalar_name))
        digest.update(_uint256_bytes(scalar_row.scalar_raw))

    return digest.hexdigest()


def digest_pool_observations(rows: Iterable[PoolBalanceDigestRow]) -> str:
    """Hash direct pool token balances independently of input ordering."""

    canonical = sorted(
        rows,
        key=lambda row: (
            _address_bytes(row.pool_address),
            _address_bytes(row.token_address),
            row.balance_raw,
        ),
    )
    digest = hashlib.sha256()
    digest.update(b"rpc-state-indexer/pool-observations/v1\x00")

    for row in canonical:
        digest.update(b"P")
        digest.update(_address_bytes(row.pool_address))
        digest.update(_address_bytes(row.token_address))
        digest.update(_uint256_bytes(row.balance_raw))

    return digest.hexdigest()


def digest_cl_observations(
    state: PoolClStateDigestRow,
    ticks: Iterable[PoolTickDigestRow],
) -> str:
    """Hash a pool's CL state and its initialized ticks, ordering-independent.

    Signed fields (``current_tick``, per-tick ``tick`` and ``liquidity_net``) use the signed
    encoder; a botched sign therefore changes the digest and blocks publication.
    """
    digest = hashlib.sha256()
    digest.update(b"rpc-state-indexer/cl-observations/v1\x00")

    digest.update(b"C")
    digest.update(_address_bytes(state.pool_address))
    digest.update(_uint256_bytes(state.sqrt_price_x96))
    digest.update(_int256_bytes(state.current_tick))
    digest.update(_uint256_bytes(state.liquidity))
    digest.update(_uint256_bytes(state.fee_growth_global_0_x128))
    digest.update(_uint256_bytes(state.fee_growth_global_1_x128))
    digest.update(_int256_bytes(state.tick_spacing))
    digest.update(_uint256_bytes(state.fee))
    digest.update(_uint256_bytes(state.tick_count))

    for row in sorted(ticks, key=lambda item: item.tick):
        if row.pool_address != state.pool_address:
            raise ValueError("tick row pool address does not match the state row")
        digest.update(b"T")
        digest.update(_int256_bytes(row.tick))
        digest.update(_uint256_bytes(row.liquidity_gross))
        digest.update(_int256_bytes(row.liquidity_net))
        digest.update(_uint256_bytes(row.fee_growth_outside_0_x128))
        digest.update(_uint256_bytes(row.fee_growth_outside_1_x128))

    return digest.hexdigest()
