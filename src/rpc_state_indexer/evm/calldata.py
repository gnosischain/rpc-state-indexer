"""Canonical selectors and fixed-width calldata helpers."""

from __future__ import annotations

import re

from eth_utils.crypto import keccak

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")
GET_BLOCK_NUMBER_SELECTOR = bytes.fromhex("42cbb15c")
GET_CURRENT_BLOCK_TIMESTAMP_SELECTOR = bytes.fromhex("0f28c97d")
GET_BLOCK_HASH_SELECTOR = bytes.fromhex("ee82ac5e")
BALANCE_OF_SELECTOR = bytes.fromhex("70a08231")
TOTAL_SUPPLY_SELECTOR = bytes.fromhex("18160ddd")
# Balancer Vault reserve reads. V2 keys pools by bytes32 poolId; V3 by pool address.
GET_POOL_TOKENS_SELECTOR = keccak(text="getPoolTokens(bytes32)")[:4]
GET_POOL_TOKEN_INFO_SELECTOR = keccak(text="getPoolTokenInfo(address)")[:4]

# Concentrated-liquidity state reads (Uniswap V3 + Algebra/Swapr V3). The ticks() and
# tick bitmap return layouts are confirmed identical/documented in [[cl-liquidity-profile]];
# the bitmap *key* convention differs by protocol ([[cl-bitmap-convention-differs]]).
SLOT0_SELECTOR = keccak(text="slot0()")[:4]
GLOBAL_STATE_SELECTOR = keccak(text="globalState()")[:4]
LIQUIDITY_SELECTOR = keccak(text="liquidity()")[:4]
TICK_SPACING_SELECTOR = keccak(text="tickSpacing()")[:4]
FEE_SELECTOR = keccak(text="fee()")[:4]
FEE_GROWTH_GLOBAL_0_SELECTOR = keccak(text="feeGrowthGlobal0X128()")[:4]
FEE_GROWTH_GLOBAL_1_SELECTOR = keccak(text="feeGrowthGlobal1X128()")[:4]
TOTAL_FEE_GROWTH_0_SELECTOR = keccak(text="totalFeeGrowth0Token()")[:4]
TOTAL_FEE_GROWTH_1_SELECTOR = keccak(text="totalFeeGrowth1Token()")[:4]
TICKS_SELECTOR = keccak(text="ticks(int24)")[:4]
TICK_BITMAP_SELECTOR = keccak(text="tickBitmap(int16)")[:4]
TICK_TABLE_SELECTOR = keccak(text="tickTable(int16)")[:4]

# TickMath bounds (shared by Uniswap V3 and Algebra).
MIN_TICK = -887272
MAX_TICK = 887272


def function_selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def signed_word(value: int, *, bits: int) -> bytes:
    """ABI-encode a signed integer as a 32-byte, sign-extended two's-complement word.

    ``bits`` bounds the value (e.g. int24 tick, int16 bitmap word position). Out-of-range
    inputs raise rather than silently wrap.
    """
    if bits <= 0 or bits > 256:
        raise ValueError(f"unsupported signed width: {bits}")
    low, high = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
    if not low <= value <= high:
        raise ValueError(f"value {value} outside int{bits} range [{low}, {high}]")
    return (value & (2**256 - 1)).to_bytes(32, "big")


def ticks_calldata(tick: int) -> bytes:
    """``ticks(int24 tick)`` for both Uniswap V3 and Algebra pools."""
    return TICKS_SELECTOR + signed_word(tick, bits=24)


def tick_bitmap_calldata(word_position: int) -> bytes:
    """Uniswap V3 ``tickBitmap(int16 wordPosition)`` (compressed-tick keyed)."""
    return TICK_BITMAP_SELECTOR + signed_word(word_position, bits=16)


def tick_table_calldata(word_position: int) -> bytes:
    """Algebra ``tickTable(int16 wordPosition)`` (raw-tick keyed)."""
    return TICK_TABLE_SELECTOR + signed_word(word_position, bits=16)


def address_word(address: str) -> bytes:
    if not ADDRESS_RE.fullmatch(address):
        raise ValueError(f"invalid EVM address: {address!r}")
    return bytes.fromhex(address[2:]).rjust(32, b"\x00")


def bytes32_word(value: str) -> bytes:
    if not HASH_RE.fullmatch(value):
        raise ValueError(f"invalid 32-byte value: {value!r}")
    return bytes.fromhex(value[2:])


def uint256_word(value: int) -> bytes:
    if value < 0 or value >= 2**256:
        raise ValueError("value is outside uint256")
    return value.to_bytes(32, "big")


def balance_of_calldata(holder: str) -> bytes:
    return BALANCE_OF_SELECTOR + address_word(holder)


def get_block_hash_calldata(block_number: int) -> bytes:
    return GET_BLOCK_HASH_SELECTOR + uint256_word(block_number)


def get_pool_tokens_calldata(pool_id: str) -> bytes:
    """Balancer V2 ``Vault.getPoolTokens(bytes32 poolId)``."""
    return GET_POOL_TOKENS_SELECTOR + bytes32_word(pool_id)


def get_pool_token_info_calldata(pool_address: str) -> bytes:
    """Balancer V3 ``Vault.getPoolTokenInfo(address pool)``."""
    return GET_POOL_TOKEN_INFO_SELECTOR + address_word(pool_address)
