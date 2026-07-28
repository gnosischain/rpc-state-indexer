from __future__ import annotations

from typing import Any, cast

import pytest

from rpc_state_indexer.collectors.metadata import (
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_RESOLVED,
    TokenMetadataCollector,
)
from rpc_state_indexer.domain import BlockRef, ExecutorKind
from rpc_state_indexer.evm.metadata_decoding import (
    MAX_TEXT_LENGTH,
    decode_decimals_return,
    decode_text_return,
)
from rpc_state_indexer.execution.base import (
    RawCallResult,
    VerificationEvidence,
    VerifiedBatchResult,
)

ANCHOR = BlockRef(25_000_000, "0x" + "aa" * 32, "0x" + "bb" * 32, 1)
TOKEN = "0x" + "11" * 20
OTHER = "0x" + "22" * 20


def word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def dynamic_string(text: str, *, offset: int = 32) -> bytes:
    body = text.encode()
    padded = body + b"\x00" * ((32 - len(body) % 32) % 32)
    return word(offset) + word(len(body)) + padded


def bytes32_text(text: str) -> bytes:
    return text.encode().ljust(32, b"\x00")


# ---------------------------------------------------------------- text decoding


def test_decodes_modern_dynamic_string() -> None:
    observed = decode_text_return(True, dynamic_string("USDC"))
    assert observed.value == "USDC"
    assert observed.encoding == "string"


def test_decodes_legacy_bytes32_symbol() -> None:
    # MKR and its contemporaries return a NUL-padded bytes32, not a string.
    observed = decode_text_return(True, bytes32_text("MKR"))
    assert observed.value == "MKR"
    assert observed.encoding == "bytes32"


def test_reverted_and_empty_returns_are_absent_not_empty_string() -> None:
    for observed in (
        decode_text_return(False, dynamic_string("X")),
        decode_text_return(True, b""),
    ):
        assert observed.value is None
        assert observed.encoding == "absent"
        assert observed.detail


def test_rejects_non_utf8_and_control_characters() -> None:
    assert decode_text_return(True, bytes32_text("A") [:31] + b"\xff").value is None
    assert decode_text_return(True, bytes32_text("A\x07B")).value is None


def test_rejects_absurdly_long_and_misaligned_strings() -> None:
    too_long = decode_text_return(True, dynamic_string("z" * (MAX_TEXT_LENGTH + 1)))
    assert too_long.value is None
    misaligned = decode_text_return(True, dynamic_string("OK", offset=33))
    assert misaligned.value is None
    # length header longer than the payload actually present
    assert decode_text_return(True, word(32) + word(64) + b"\x01" * 8).value is None


def test_all_nul_word_is_absent() -> None:
    assert decode_text_return(True, word(0)).value is None


# ------------------------------------------------------------ decimals decoding


def test_decimals_zero_is_a_real_answer_not_unknown() -> None:
    # The whole point of Nullable: 0 decimals is legitimate and must be distinguishable
    # from "not observed", or a balance is silently rendered unscaled.
    assert decode_decimals_return(True, word(0)) == 0
    assert decode_decimals_return(True, word(18)) == 18


def test_decimals_unobservable_cases_return_none() -> None:
    assert decode_decimals_return(False, word(18)) is None       # reverted
    assert decode_decimals_return(True, b"") is None             # empty
    assert decode_decimals_return(True, b"\x12") is None         # short
    assert decode_decimals_return(True, word(255)) is None       # implausible
    assert decode_decimals_return(True, word(2**200)) is None    # nonsense


# ------------------------------------------------------------------- collector


class FakeExecutor:
    """Returns per-key canned results, mimicking Multicall3 allow_failure semantics."""

    def __init__(self, canned: dict[str, tuple[bool, bytes]], *, drop: set[str] | None = None):
        self.canned = canned
        self.drop = drop or set()
        self.calls: list[str] = []

    async def execute(self, calls: Any, anchor: BlockRef) -> list[VerifiedBatchResult]:
        results = []
        for call in calls:
            self.calls.append(call.key)
            if call.key in self.drop:
                continue
            success, data = self.canned.get(call.key, (False, b""))
            results.append(RawCallResult(call.key, success, data))
        evidence = VerificationEvidence(
            ExecutorKind.MULTICALL3, "eip1898", anchor.block_hash, ("g",), "c" * 64, True
        )
        return [VerifiedBatchResult(tuple(results), evidence)]


def collector(executor: FakeExecutor) -> TokenMetadataCollector:
    return TokenMetadataCollector(cast(Any, executor), chain_id=100)


@pytest.mark.asyncio
async def test_fully_resolved_token() -> None:
    ex = FakeExecutor({
        f"{TOKEN}:symbol": (True, dynamic_string("GNO")),
        f"{TOKEN}:name": (True, dynamic_string("Gnosis Token")),
        f"{TOKEN}:decimals": (True, word(18)),
    })
    (row,) = await collector(ex).resolve((TOKEN,), ANCHOR)
    assert (row.symbol, row.name, row.decimals) == ("GNO", "Gnosis Token", 18)
    assert row.resolution_status == STATUS_RESOLVED
    assert row.anchor_block == ANCHOR.number and row.anchor_hash == ANCHOR.block_hash
    assert row.error_class == ""


@pytest.mark.asyncio
async def test_partial_resolution_keeps_what_was_observed() -> None:
    ex = FakeExecutor({
        f"{TOKEN}:symbol": (True, dynamic_string("WEIRD")),
        f"{TOKEN}:name": (False, b""),          # reverts
        f"{TOKEN}:decimals": (True, word(6)),
    })
    (row,) = await collector(ex).resolve((TOKEN,), ANCHOR)
    assert row.symbol == "WEIRD" and row.decimals == 6
    assert row.name is None
    assert row.resolution_status == STATUS_PARTIAL
    assert "name" in row.error_message


@pytest.mark.asyncio
async def test_total_failure_is_recorded_and_never_raises() -> None:
    """A token that implements none of it must not break admission."""

    ex = FakeExecutor({})  # every call reverts
    (row,) = await collector(ex).resolve((TOKEN,), ANCHOR)
    assert (row.symbol, row.name, row.decimals) == (None, None, None)
    assert row.resolution_status == STATUS_FAILED
    assert row.error_class == "UnresolvedMetadata"


@pytest.mark.asyncio
async def test_missing_call_result_is_recorded_not_invented() -> None:
    ex = FakeExecutor(
        {f"{TOKEN}:symbol": (True, dynamic_string("A"))},
        drop={f"{TOKEN}:decimals"},
    )
    (row,) = await collector(ex).resolve((TOKEN,), ANCHOR)
    assert row.resolution_status == STATUS_FAILED
    assert row.error_class == "MissingCallResult"
    assert row.decimals is None


@pytest.mark.asyncio
async def test_addresses_are_deduplicated_and_ordered() -> None:
    ex = FakeExecutor({})
    rows = await collector(ex).resolve((OTHER, TOKEN, OTHER), ANCHOR)
    assert [row.token_address for row in rows] == sorted({TOKEN, OTHER})
    assert len(ex.calls) == 6  # 3 calls x 2 unique addresses


@pytest.mark.asyncio
async def test_empty_input_makes_no_calls() -> None:
    ex = FakeExecutor({})
    assert await collector(ex).resolve((), ANCHOR) == ()
    assert ex.calls == []


def test_row_shape_matches_the_table_contract() -> None:
    from rpc_state_indexer.storage.repositories import TABLE_COLUMNS

    ex = FakeExecutor({})
    import asyncio

    (row,) = asyncio.run(collector(ex).resolve((TOKEN,), ANCHOR))
    assert set(row.as_row()) == set(TABLE_COLUMNS["token_metadata"])
