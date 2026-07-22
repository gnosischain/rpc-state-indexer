from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from pydantic import ValidationError

from rpc_state_indexer.collectors.balancer import BalancerPoolCollector
from rpc_state_indexer.config.models import PoolAssetConfig, PoolConfig
from rpc_state_indexer.domain import BlockRef, ExecutorKind, IntegrityMode, ObservationStatus
from rpc_state_indexer.evm.calldata import (
    GET_POOL_TOKEN_INFO_SELECTOR,
    GET_POOL_TOKENS_SELECTOR,
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
    RawCallResult,
    VerificationEvidence,
    VerifiedBatchResult,
    digest_raw_results,
)

V2_VAULT = "0x" + "a1" * 20
V3_VAULT = "0x" + "a3" * 20
POOL = "0x" + "b0" * 20
TOKEN_A = "0x" + "11" * 20
TOKEN_B = "0x" + "22" * 20
BPT = "0x" + "b0" * 20  # a composable pool's own token equals the pool address
POOL_ID = "0x" + "cd" * 32
ANCHOR = BlockRef(100, "0x" + "ab" * 32, "0x" + "cd" * 32, 1234)


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _addr_word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def encode_v2(tokens: list[str], balances: list[int]) -> bytes:
    off_balances = 96 + 32 * (1 + len(tokens))
    head = _word(96) + _word(off_balances) + _word(999)  # tokens, balances, lastChangeBlock
    tokens_arr = _word(len(tokens)) + b"".join(_addr_word(t) for t in tokens)
    balances_arr = _word(len(balances)) + b"".join(_word(b) for b in balances)
    return head + tokens_arr + balances_arr


def encode_v3(tokens: list[str], balances_raw: list[int]) -> bytes:
    tokens_arr = _word(len(tokens)) + b"".join(_addr_word(t) for t in tokens)
    # TokenInfo[] of static 3-word structs; content is irrelevant to the decoder.
    info_arr = _word(len(tokens)) + b"".join(
        _word(0) + _addr_word("0x" + "00" * 20) + _word(0) for _ in tokens
    )
    bal_arr = _word(len(balances_raw)) + b"".join(_word(b) for b in balances_raw)
    scaled_arr = _word(len(balances_raw)) + b"".join(_word(1) for _ in balances_raw)
    off_t = 128
    off_i = off_t + len(tokens_arr)
    off_b = off_i + len(info_arr)
    off_s = off_b + len(bal_arr)
    head = _word(off_t) + _word(off_i) + _word(off_b) + _word(off_s)
    return head + tokens_arr + info_arr + bal_arr + scaled_arr


# ---------------------------------------------------------------- calldata

def test_calldata_selectors_and_shape() -> None:
    assert GET_POOL_TOKENS_SELECTOR.hex() == "f94d4668"
    assert get_pool_tokens_calldata(POOL_ID) == GET_POOL_TOKENS_SELECTOR + bytes.fromhex(
        POOL_ID[2:]
    )
    info = get_pool_token_info_calldata(POOL)
    assert info[:4] == GET_POOL_TOKEN_INFO_SELECTOR
    assert info[4:] == _addr_word(POOL)


# ---------------------------------------------------------------- decoders

def test_v2_decoder_valid() -> None:
    out = decode_balancer_v2_pool_tokens(encode_v2([TOKEN_A, TOKEN_B], [1000, 2000]))
    assert out == ((TOKEN_A, 1000), (TOKEN_B, 2000))


def test_v3_decoder_picks_balances_raw_skipping_tokeninfo() -> None:
    out = decode_balancer_v3_pool_token_info(encode_v3([TOKEN_A, TOKEN_B], [7, 8]))
    assert out == ((TOKEN_A, 7), (TOKEN_B, 8))


def test_decoder_rejects_length_mismatch() -> None:
    head = _word(96) + _word(192) + _word(0)
    body = _word(2) + _addr_word(TOKEN_A) + _addr_word(TOKEN_B) + _word(1) + _word(5)
    with pytest.raises(BalancerDecodeError):
        decode_balancer_v2_pool_tokens(head + body)


def test_decoder_rejects_short_return() -> None:
    with pytest.raises(BalancerDecodeError):
        decode_balancer_v2_pool_tokens(b"\x00" * 32)


def test_decoder_rejects_non_canonical_address() -> None:
    dirty = bytes([0xFF]) + bytes(11) + bytes.fromhex(TOKEN_A[2:])
    raw = _word(96) + _word(160) + _word(0) + _word(1) + dirty + _word(1) + _word(5)
    with pytest.raises(BalancerDecodeError):
        decode_balancer_v2_pool_tokens(raw)


def test_decoder_rejects_empty_pool() -> None:
    raw = _word(96) + _word(128) + _word(0) + _word(0) + _word(0)
    with pytest.raises(BalancerDecodeError):
        decode_balancer_v2_pool_tokens(raw)


# ---------------------------------------------------------------- collector

class FakeExecutor:
    def __init__(self, responses: Mapping[str, bytes | tuple[int, str]]) -> None:
        self.responses = responses
        self.seen: tuple[ContractCall, ...] = ()

    async def execute(
        self, calls: Sequence[ContractCall], anchor: BlockRef
    ) -> list[VerifiedBatchResult]:
        self.seen = tuple(calls)
        results = []
        for call in calls:
            resp = self.responses[call.key]
            if isinstance(resp, tuple):
                results.append(RawCallResult(call.key, False, b"", resp[0], resp[1]))
            else:
                results.append(RawCallResult(call.key, True, resp))
        body = tuple(results)
        return [
            VerifiedBatchResult(
                body,
                VerificationEvidence(
                    executor_kind=ExecutorKind.MULTICALL3,
                    block_reference_kind="eip1898",
                    anchor_hash=anchor.block_hash,
                    provider_groups=("fake",),
                    result_digest=digest_raw_results(body),
                    verified=True,
                ),
            )
        ]


def v2_pool() -> PoolConfig:
    return PoolConfig(
        address=POOL,
        name="A-B v2",
        pool_class="balancer_v2",
        deployment_block=1,
        pool_id=POOL_ID,
        assets=[PoolAssetConfig(token=TOKEN_A), PoolAssetConfig(token=TOKEN_B)],
    )


def v3_pool() -> PoolConfig:
    return PoolConfig(
        address=POOL,
        name="A-B v3",
        pool_class="balancer_v3",
        deployment_block=1,
        assets=[PoolAssetConfig(token=TOKEN_A), PoolAssetConfig(token=TOKEN_B)],
    )


def collector(responses: Mapping[str, bytes | tuple[int, str]]) -> BalancerPoolCollector:
    return BalancerPoolCollector(FakeExecutor(responses), v2_vault=V2_VAULT, v3_vault=V3_VAULT)


@pytest.mark.asyncio
async def test_v2_collect_targets_vault_and_publishes() -> None:
    exe = FakeExecutor({f"balancer/{POOL}": encode_v2([TOKEN_A, TOKEN_B], [10, 20])})
    coll = BalancerPoolCollector(exe, v2_vault=V2_VAULT, v3_vault=V3_VAULT)
    result = await coll.collect(pool=v2_pool(), anchor=ANCHOR)
    assert exe.seen[0].target == V2_VAULT
    assert exe.seen[0].calldata[:4] == GET_POOL_TOKENS_SELECTOR
    assert result.verified
    assert {(b.token_address, b.balance_raw) for b in result.balances} == {
        (TOKEN_A, 10),
        (TOKEN_B, 20),
    }
    assert coll.vault_target(v2_pool()) == V2_VAULT


@pytest.mark.asyncio
async def test_v3_collect_targets_vault_and_publishes() -> None:
    exe = FakeExecutor({f"balancer/{POOL}": encode_v3([TOKEN_A, TOKEN_B], [3, 4])})
    coll = BalancerPoolCollector(exe, v2_vault=V2_VAULT, v3_vault=V3_VAULT)
    result = await coll.collect(pool=v3_pool(), anchor=ANCHOR)
    assert exe.seen[0].target == V3_VAULT
    assert exe.seen[0].calldata[:4] == GET_POOL_TOKEN_INFO_SELECTOR
    assert result.verified
    assert result.balances[0].balance_raw in {3, 4}


@pytest.mark.asyncio
async def test_composable_pool_extra_bpt_is_ignored() -> None:
    # Vault returns three tokens (incl. the pool's own BPT); config lists the two underlyings.
    raw = encode_v2([TOKEN_A, BPT, TOKEN_B], [10, 999, 20])
    result = await collector({f"balancer/{POOL}": raw}).collect(pool=v2_pool(), anchor=ANCHOR)
    assert result.verified
    assert {(b.token_address, b.balance_raw) for b in result.balances} == {
        (TOKEN_A, 10),
        (TOKEN_B, 20),
    }


@pytest.mark.asyncio
async def test_missing_configured_asset_is_an_error_not_a_publish() -> None:
    raw = encode_v2([TOKEN_A], [10])  # TOKEN_B configured but absent from the vault return
    result = await collector({f"balancer/{POOL}": raw}).collect(pool=v2_pool(), anchor=ANCHOR)
    assert not result.verified
    assert result.errors and result.errors[0].status is ObservationStatus.MALFORMED_RETURN
    assert not result.balances


@pytest.mark.asyncio
async def test_reverted_vault_call_is_an_error_not_zero() -> None:
    result = await collector(
        {f"balancer/{POOL}": (-32000, "execution reverted")}
    ).collect(pool=v2_pool(), anchor=ANCHOR)
    assert not result.verified
    assert result.errors and result.errors[0].status is ObservationStatus.REVERTED
    assert not result.balances


@pytest.mark.asyncio
async def test_malformed_return_is_an_error_not_zero() -> None:
    result = await collector({f"balancer/{POOL}": b"\x00" * 32}).collect(
        pool=v2_pool(), anchor=ANCHOR
    )
    assert not result.verified
    assert result.errors and result.errors[0].status is ObservationStatus.MALFORMED_RETURN


@pytest.mark.asyncio
async def test_missing_vault_config_raises() -> None:
    coll = BalancerPoolCollector(FakeExecutor({}), v2_vault=None, v3_vault=V3_VAULT)
    with pytest.raises(ValueError, match="v2 vault"):
        await coll.collect(pool=v2_pool(), anchor=ANCHOR)


@pytest.mark.asyncio
async def test_non_pool_assets_integrity_rejected() -> None:
    with pytest.raises(ValueError, match="pool_assets"):
        await collector({}).collect(
            pool=v2_pool(), anchor=ANCHOR, integrity_mode=IntegrityMode.SCOPED
        )


# ---------------------------------------------------------------- config validation

def test_balancer_v2_requires_pool_id() -> None:
    with pytest.raises(ValidationError, match="pool_id"):
        PoolConfig(
            address=POOL,
            name="x",
            pool_class="balancer_v2",
            deployment_block=1,
            assets=[PoolAssetConfig(token=TOKEN_A)],
        )


def test_pool_id_only_valid_for_balancer_v2() -> None:
    with pytest.raises(ValidationError, match="pool_id"):
        PoolConfig(
            address=POOL,
            name="x",
            pool_class="balancer_v3",
            deployment_block=1,
            pool_id=POOL_ID,
            assets=[PoolAssetConfig(token=TOKEN_A)],
        )
