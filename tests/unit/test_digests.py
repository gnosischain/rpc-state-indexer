from __future__ import annotations

import pytest

from rpc_state_indexer.storage.digests import (
    BalanceDigestRow,
    PoolBalanceDigestRow,
    ScalarDigestRow,
    digest_pool_observations,
    digest_token_observations,
    digest_universe,
)

ADDRESS_A = "0x0000000000000000000000000000000000000001"
ADDRESS_B = "0x0000000000000000000000000000000000000002"
TOKEN_A = "0x1000000000000000000000000000000000000001"
TOKEN_B = "0x1000000000000000000000000000000000000002"


def test_universe_digest_is_independent_of_member_and_source_order() -> None:
    first = digest_universe(
        [
            (ADDRESS_B, ["seed", "own_scan"]),
            (ADDRESS_A, ["own_scan"]),
        ]
    )
    second = digest_universe(
        [
            (ADDRESS_A, ["own_scan"]),
            (ADDRESS_B, ["own_scan", "seed"]),
        ]
    )

    assert first == second
    assert len(first) == 64


def test_universe_digest_rejects_duplicate_members() -> None:
    with pytest.raises(ValueError, match="duplicate universe member"):
        digest_universe(
            [
                (ADDRESS_A, ["own_scan"]),
                (ADDRESS_A, ["seed"]),
            ]
        )


def test_token_digest_is_order_independent_and_preserves_observed_zero() -> None:
    balances = [
        BalanceDigestRow(ADDRESS_B, 100),
        BalanceDigestRow(ADDRESS_A, 0),
    ]
    scalars = [ScalarDigestRow("total_supply", 100)]

    expected = digest_token_observations(balances, scalars)
    reordered = digest_token_observations(reversed(balances), reversed(scalars))

    assert reordered == expected


def test_scaled_balance_changes_token_digest() -> None:
    direct = digest_token_observations(
        [BalanceDigestRow(ADDRESS_A, 5, None, "direct")],
        [ScalarDigestRow("total_supply", 5)],
    )
    scaled = digest_token_observations(
        [BalanceDigestRow(ADDRESS_A, 5, 5, "scaled_reconstructed")],
        [ScalarDigestRow("total_supply", 5)],
    )

    assert scaled != direct


def test_digest_rejects_values_outside_uint256() -> None:
    with pytest.raises(ValueError, match="outside UInt256"):
        digest_token_observations(
            [BalanceDigestRow(ADDRESS_A, -1)],
            [],
        )

    with pytest.raises(ValueError, match="outside UInt256"):
        digest_token_observations(
            [BalanceDigestRow(ADDRESS_A, 1 << 256)],
            [],
        )


def test_digest_rejects_non_normalized_address() -> None:
    with pytest.raises(ValueError, match="normalized lowercase"):
        digest_universe(
            [("0x00000000000000000000000000000000000000AA", ["seed"])]
        )


def test_pool_digest_is_order_independent() -> None:
    rows = [
        PoolBalanceDigestRow(ADDRESS_A, TOKEN_B, 9),
        PoolBalanceDigestRow(ADDRESS_A, TOKEN_A, 4),
    ]

    assert digest_pool_observations(rows) == digest_pool_observations(reversed(rows))
