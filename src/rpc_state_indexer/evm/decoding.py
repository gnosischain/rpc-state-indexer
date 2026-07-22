"""Strict scalar decoding: failure and zero are never conflated."""

from __future__ import annotations

from dataclasses import dataclass

from rpc_state_indexer.domain import ObservationStatus, UIntObservation


def hex_data_to_bytes(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("RPC data must be 0x-prefixed hex")
    body = value[2:]
    if len(body) % 2:
        raise ValueError("RPC hex data must contain whole bytes")
    try:
        return bytes.fromhex(body)
    except ValueError as exc:
        raise ValueError("RPC data contains non-hex characters") from exc


def decode_uint256(success: bool, returndata: bytes | str) -> UIntObservation:
    try:
        raw = hex_data_to_bytes(returndata) if isinstance(returndata, str) else bytes(returndata)
    except (TypeError, ValueError) as exc:
        return UIntObservation(ObservationStatus.MALFORMED_RETURN, None, detail=str(exc))
    if not success:
        return UIntObservation(ObservationStatus.REVERTED, None, raw)
    if not raw:
        return UIntObservation(ObservationStatus.EMPTY_RETURN, None, raw)
    if len(raw) != 32:
        return UIntObservation(
            ObservationStatus.MALFORMED_RETURN,
            None,
            raw,
            f"expected 32 bytes, got {len(raw)}",
        )
    return UIntObservation(ObservationStatus.OK, int.from_bytes(raw, "big"), raw)


_WORD = 32


class BalancerDecodeError(ValueError):
    """A Balancer Vault return did not match its expected ABI layout.

    Raised, never swallowed: a structural anomaly is a hard failure, not an empty or
    zero pool. Callers materialize it as a MALFORMED_RETURN observation.
    """


def _read_offset(raw: bytes, index: int) -> int:
    start = index * _WORD
    if start + _WORD > len(raw):
        raise BalancerDecodeError(f"return data too short for head word {index}")
    offset = int.from_bytes(raw[start : start + _WORD], "big")
    if offset % _WORD != 0:
        raise BalancerDecodeError(f"array offset {offset} is not word-aligned")
    return offset


def _read_array_words(raw: bytes, offset: int) -> list[bytes]:
    if offset + _WORD > len(raw):
        raise BalancerDecodeError("array length word is out of bounds")
    length = int.from_bytes(raw[offset : offset + _WORD], "big")
    data_start = offset + _WORD
    data_end = data_start + length * _WORD
    if data_end > len(raw):
        raise BalancerDecodeError("array element data is out of bounds")
    return [raw[data_start + i * _WORD : data_start + (i + 1) * _WORD] for i in range(length)]


def _word_to_address(word: bytes) -> str:
    if word[:12] != b"\x00" * 12:
        raise BalancerDecodeError("address word has non-zero high bytes")
    return "0x" + word[12:].hex()


def _zip_tokens_balances(
    raw: bytes, tokens_offset: int, balances_offset: int
) -> tuple[tuple[str, int], ...]:
    token_words = _read_array_words(raw, tokens_offset)
    balance_words = _read_array_words(raw, balances_offset)
    if len(token_words) != len(balance_words):
        raise BalancerDecodeError(
            f"tokens/balances length mismatch: {len(token_words)} vs {len(balance_words)}"
        )
    if not token_words:
        raise BalancerDecodeError("pool reports no tokens")
    return tuple(
        (_word_to_address(token), int.from_bytes(balance, "big"))
        for token, balance in zip(token_words, balance_words, strict=True)
    )


def decode_balancer_v2_pool_tokens(returndata: bytes) -> tuple[tuple[str, int], ...]:
    """Decode V2 ``getPoolTokens`` → ``(address[] tokens, uint256[] balances, uint256)``.

    Returns ``((token_lower, balance), ...)``. Raises ``BalancerDecodeError`` on any anomaly.
    """
    if len(returndata) < 3 * _WORD:
        raise BalancerDecodeError("return data too short for getPoolTokens head")
    tokens_offset = _read_offset(returndata, 0)
    balances_offset = _read_offset(returndata, 1)
    return _zip_tokens_balances(returndata, tokens_offset, balances_offset)


def decode_balancer_v3_pool_token_info(returndata: bytes) -> tuple[tuple[str, int], ...]:
    """Decode V3 ``getPoolTokenInfo`` → ``(address[] tokens, TokenInfo[], uint256[]
    balancesRaw, uint256[] scaled18)``.

    Uses head offsets to read ``tokens`` (index 0) and ``balancesRaw`` (index 2), skipping the
    ``TokenInfo[]`` struct array and the scaled balances entirely. Returns raw integer amounts.
    Raises ``BalancerDecodeError`` on any anomaly.
    """
    if len(returndata) < 4 * _WORD:
        raise BalancerDecodeError("return data too short for getPoolTokenInfo head")
    tokens_offset = _read_offset(returndata, 0)
    balances_raw_offset = _read_offset(returndata, 2)
    return _zip_tokens_balances(returndata, tokens_offset, balances_raw_offset)


# --- Concentrated-liquidity primitives (Uniswap V3 + Algebra/Swapr V3) --------------
#
# Struct layouts confirmed live on Gnosis (see [[cl-liquidity-profile]]). ``slot0``/
# ``globalState`` differ; ``ticks(int24)`` is byte-identical across both protocols. Signed
# fields (int24 tick, int128 liquidityNet/Delta) are decoded sign-extended and validated —
# a botched sign is caught here, and again by the ΣliquidityNet invariants downstream.


class ClDecodeError(ValueError):
    """A concentrated-liquidity return did not match its expected ABI layout.

    Raised, never swallowed: a structural or sign-extension anomaly is a hard failure, not an
    empty or zero pool. Callers materialize it as a MALFORMED_RETURN observation.
    """


@dataclass(frozen=True, slots=True)
class DecodedSlot0:
    sqrt_price_x96: int
    tick: int


@dataclass(frozen=True, slots=True)
class DecodedGlobalState:
    sqrt_price_x96: int
    tick: int
    fee: int


@dataclass(frozen=True, slots=True)
class DecodedTick:
    liquidity_gross: int
    liquidity_net: int
    fee_growth_outside_0_x128: int
    fee_growth_outside_1_x128: int
    initialized: bool


def _word(raw: bytes, index: int) -> bytes:
    start = index * _WORD
    if start + _WORD > len(raw):
        raise ClDecodeError(f"return data too short for word {index}")
    return raw[start : start + _WORD]


def _decode_uint(word: bytes, *, bits: int) -> int:
    """A ``uint<bits>`` right-aligned in a 32-byte word; high bytes must be zero."""
    if bits % 8 or not 0 < bits <= 256:
        raise ClDecodeError(f"unsupported uint width: {bits}")
    high = _WORD - bits // 8
    if word[:high] != b"\x00" * high:
        raise ClDecodeError(f"uint{bits} word has non-zero high bytes")
    return int.from_bytes(word, "big")


def _decode_int(word: bytes, *, bits: int) -> int:
    """A signed ``int<bits>`` sign-extended across the full 32-byte word."""
    if not 0 < bits <= 256:
        raise ClDecodeError(f"unsupported int width: {bits}")
    value = int.from_bytes(word, "big")
    body = value & ((1 << bits) - 1)
    negative = bool(body & (1 << (bits - 1)))
    expected_high = (1 << (256 - bits)) - 1 if negative else 0
    if value >> bits != expected_high:
        raise ClDecodeError(f"int{bits} word is not correctly sign-extended")
    return body - (1 << bits) if negative else body


def _decode_bool(word: bytes) -> bool:
    if word[: _WORD - 1] != b"\x00" * (_WORD - 1) or word[-1] not in (0, 1):
        raise ClDecodeError("bool word is not 0 or 1")
    return word[-1] == 1


def decode_uint_return(returndata: bytes, *, bits: int) -> int:
    """Single-word unsigned scalar return (e.g. ``liquidity()`` uint128,
    ``feeGrowthGlobal0X128()`` uint256). Raises on short/oversized returns."""
    if len(returndata) < _WORD:
        raise ClDecodeError("return data too short for a scalar word")
    return _decode_uint(_word(returndata, 0), bits=bits)


def decode_int_return(returndata: bytes, *, bits: int) -> int:
    """Single-word signed scalar return (e.g. ``tickSpacing()`` int24)."""
    if len(returndata) < _WORD:
        raise ClDecodeError("return data too short for a scalar word")
    return _decode_int(_word(returndata, 0), bits=bits)


def decode_slot0(returndata: bytes) -> DecodedSlot0:
    """Uniswap V3 ``slot0()`` → (uint160 sqrtPriceX96, int24 tick, ...). Reads words 0-1."""
    return DecodedSlot0(
        sqrt_price_x96=_decode_uint(_word(returndata, 0), bits=160),
        tick=_decode_int(_word(returndata, 1), bits=24),
    )


def decode_global_state(returndata: bytes) -> DecodedGlobalState:
    """Algebra ``globalState()`` → (uint160 price, int24 tick, uint16 fee, ...). Reads 0-2."""
    return DecodedGlobalState(
        sqrt_price_x96=_decode_uint(_word(returndata, 0), bits=160),
        tick=_decode_int(_word(returndata, 1), bits=24),
        fee=_decode_uint(_word(returndata, 2), bits=16),
    )


def decode_ticks(returndata: bytes) -> DecodedTick:
    """``ticks(int24)`` → 8-word struct, identical for Uniswap V3 and Algebra.

    word0 liquidityGross/Total (uint128), word1 **liquidityNet/Delta (int128, signed)**,
    word2/3 feeGrowthOutside0/1 (uint256), word7 initialized (bool). Middle words (tick
    cumulatives, seconds) are not ingested.
    """
    if len(returndata) < 8 * _WORD:
        raise ClDecodeError("return data too short for ticks() struct")
    return DecodedTick(
        liquidity_gross=_decode_uint(_word(returndata, 0), bits=128),
        liquidity_net=_decode_int(_word(returndata, 1), bits=128),
        fee_growth_outside_0_x128=int.from_bytes(_word(returndata, 2), "big"),
        fee_growth_outside_1_x128=int.from_bytes(_word(returndata, 3), "big"),
        initialized=_decode_bool(_word(returndata, 7)),
    )


def decode_bitmap_word(returndata: bytes) -> tuple[int, ...]:
    """A tick-bitmap word (``tickBitmap``/``tickTable``) → the set bit positions [0, 256).

    The caller maps (wordPosition, bit) back to a tick per the protocol's key convention
    ([[cl-bitmap-convention-differs]]): Uniswap compresses by tickSpacing, Algebra does not.
    """
    if len(returndata) != _WORD:
        raise ClDecodeError(f"bitmap word must be exactly 32 bytes, got {len(returndata)}")
    value = int.from_bytes(returndata, "big")
    return tuple(bit for bit in range(256) if (value >> bit) & 1)

