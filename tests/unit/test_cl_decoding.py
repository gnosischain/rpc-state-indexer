"""Concentrated-liquidity decoder tests, verified against live Gnosis returndata.

The golden vectors below were captured 2026-07-22 via raw eth_call against a liquid Uniswap
V3 pool (0x01343cf4…, tickSpacing 10, current tick 1603) and a Swapr V3 / Algebra pool
(0xa3c906c6…, tickSpacing 60, current tick -54078). They exercise real signed int128
liquidityNet/Delta (both signs) and the two distinct bitmap key conventions.
"""

from __future__ import annotations

import pytest

from rpc_state_indexer.evm.calldata import (
    TICK_BITMAP_SELECTOR,
    TICK_TABLE_SELECTOR,
    TICKS_SELECTOR,
    signed_word,
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


def _b(hexstr: str) -> bytes:
    return bytes.fromhex(hexstr.removeprefix("0x"))


# --- live golden vectors ------------------------------------------------------------
UNI_SLOT0 = (
    "0x0000000000000000000000000000000000000001155cfb5a057872a1d6ebe328"
    "0000000000000000000000000000000000000000000000000000000000000643"
    "0000000000000000000000000000000000000000000000000000000000000095"
    "00000000000000000000000000000000000000000000000000000000000000c8"
    "00000000000000000000000000000000000000000000000000000000000000c8"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000001"
)
# ticks(1610): upper boundary -> liquidityNet NEGATIVE
UNI_TICKS_1610 = (
    "0x00000000000000000000000000000000000000000035bd72c32c94b11af5c525"
    "ffffffffffffffffffffffffffffffffffffffffffca428d3cd36b4ee50a3adb"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000001"
)
# ticks(1570): lower boundary -> liquidityNet POSITIVE, with nonzero fee growth outside
UNI_TICKS_1570 = (
    "0x00000000000000000000000000000000000000000035bd72c32c94b11af5c525"
    "00000000000000000000000000000000000000000035bd72c32c94b11af5c525"
    "0000000000000000000000000000000001529305d260f413897b02884c3c4552"
    "000000000000000000000000000000005fc6a595e0f1dd7974ebe376a5d50cb7"
    "0000000000000000000000000000000000000000000000000000001823d1cb68"
    "000000000000000000000000000000190000014528d29cf99e7ce38e26a34c42"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000001"
)
UNI_TICKBITMAP_0 = "0x0000000000000000000000522000000000000000000000000000000000000000"

ALG_GLOBALSTATE = (
    "0x00000000000000000000000000000000000000001123d91cc8962d973426aa4c"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff2cc2"
    "00000000000000000000000000000000000000000000000000000000000007d0"
    "0000000000000000000000000000000000000000000000000000000000002529"
    "0000000000000000000000000000000000000000000000000000000000000064"
    "0000000000000000000000000000000000000000000000000000000000000064"
    "0000000000000000000000000000000000000000000000000000000000000001"
)
# ticks(-54360): liquidityDelta NEGATIVE
ALG_TICKS_NEG = (
    "0x0000000000000000000000000000000000000000000000028b87ba3bb1de5e4b"
    "fffffffffffffffffffffffffffffffffffffffffffffffd747845c44e21a1b5"
    "000000000000000000000000000000002da6e7a55523d8d9aacd657413732174"
    "00000000000000000000000000000000005b0213342e6b3f889809e7136aea45"
    "fffffffffffffffffffffffffffffffffffffffffffffffffffffe513075985e"
    "000000000000000000000000000000000000000000123c4d58e521dfffee9c3b"
    "000000000000000000000000000000000000000000000000000000006768b859"
    "0000000000000000000000000000000000000000000000000000000000000001"
)
ALG_TICKTABLE_NEG213 = (
    "0x0000000000000000000001000000000000000000000000000000000000000000"
)


def test_uniswap_slot0_decodes_positive_tick() -> None:
    state = decode_slot0(_b(UNI_SLOT0))
    assert state.tick == 1603
    assert state.sqrt_price_x96 == 0x01155CFB5A057872A1D6EBE328


def test_algebra_global_state_decodes_negative_tick_and_fee() -> None:
    state = decode_global_state(_b(ALG_GLOBALSTATE))
    assert state.tick == -54078
    assert state.fee == 2000
    assert state.sqrt_price_x96 == 0x1123D91CC8962D973426AA4C


def test_uniswap_ticks_negative_liquidity_net() -> None:
    tick = decode_ticks(_b(UNI_TICKS_1610))
    assert tick.liquidity_gross == 64967712697441336847353125
    assert tick.liquidity_net == -64967712697441336847353125
    assert tick.initialized is True
    assert tick.fee_growth_outside_0_x128 == 0


def test_uniswap_ticks_positive_liquidity_net_and_fee_growth() -> None:
    tick = decode_ticks(_b(UNI_TICKS_1570))
    assert tick.liquidity_net == 64967712697441336847353125
    assert tick.liquidity_gross == 64967712697441336847353125
    assert tick.fee_growth_outside_0_x128 > 0
    assert tick.fee_growth_outside_1_x128 > 0


def test_algebra_ticks_negative_liquidity_delta() -> None:
    tick = decode_ticks(_b(ALG_TICKS_NEG))
    assert tick.liquidity_gross == 46947697606097002059
    assert tick.liquidity_net == -46947697606097002059
    assert tick.initialized is True


def test_sum_of_paired_boundary_nets_is_zero() -> None:
    """A position's lower(+net) and upper(-net) boundaries cancel — the Σnet==0 invariant."""
    lower = decode_ticks(_b(UNI_TICKS_1570)).liquidity_net
    upper = decode_ticks(_b(UNI_TICKS_1610)).liquidity_net
    assert lower + upper == 0


def test_uniswap_bitmap_is_compressed_key() -> None:
    # word 0 holds compressed ticks; 1570/1610/1640/1660 at spacing 10 -> 157/161/164/166.
    bits = decode_bitmap_word(_b(UNI_TICKBITMAP_0))
    assert {157, 161, 164, 166}.issubset(set(bits))


def test_algebra_bitmap_is_raw_key() -> None:
    # tickTable(-213) holds raw tick -54360 -> bit (-54360 & 0xff) = 168.
    bits = decode_bitmap_word(_b(ALG_TICKTABLE_NEG213))
    assert bits == (168,)
    assert (-54360 >> 8) == -213 and (-54360 & 0xFF) == 168


def test_scalar_returns() -> None:
    assert decode_uint_return(_b("0x" + "00" * 31 + "0a"), bits=128) == 10
    assert decode_int_return(signed_word(60, bits=24), bits=24) == 60
    assert decode_int_return(signed_word(-887272, bits=24), bits=24) == -887272


# --- strictness: failures never silently become zero --------------------------------
def test_short_return_raises_not_zero() -> None:
    with pytest.raises(ClDecodeError):
        decode_ticks(_b("0x" + "00" * 32))  # one word, need eight


def test_bad_sign_extension_raises() -> None:
    # int128 body says negative (top body bit set) but high 128 bits are zero -> corrupt.
    corrupt = "00" * 16 + "80" + "00" * 15  # word1-style, wrong extension
    payload = ("00" * 32) + corrupt + ("00" * 32) * 6
    with pytest.raises(ClDecodeError):
        decode_ticks(_b("0x" + payload))


def test_uint_with_dirty_high_bytes_raises() -> None:
    with pytest.raises(ClDecodeError):
        decode_uint_return(_b("0x" + "ff" + "00" * 31), bits=128)


def test_non_word_bitmap_raises() -> None:
    with pytest.raises(ClDecodeError):
        decode_bitmap_word(_b("0x" + "00" * 31))


def test_signed_word_bounds() -> None:
    assert signed_word(-1, bits=24) == b"\xff" * 32
    with pytest.raises(ValueError):
        signed_word(2**23, bits=24)  # int24 max is 2**23 - 1
    with pytest.raises(ValueError):
        signed_word(-(2**15) - 1, bits=16)


def test_calldata_builders_roundtrip_selector_and_arg() -> None:
    assert ticks_calldata(1610)[:4] == TICKS_SELECTOR
    assert ticks_calldata(-54360)[4:] == signed_word(-54360, bits=24)
    assert tick_bitmap_calldata(0)[:4] == TICK_BITMAP_SELECTOR
    assert tick_table_calldata(-213)[:4] == TICK_TABLE_SELECTOR
    assert tick_table_calldata(-213)[4:] == signed_word(-213, bits=16)
