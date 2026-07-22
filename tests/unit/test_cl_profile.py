"""ClProfileModule reconstruction walk, verified against the ΣliquidityNet structure.

A fake store serves published state/tick rows and captures the derived profile rows. Uses
the same two-position shape as the live pools: lower(+net) / upper(-net) boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from rpc_state_indexer.compute import REGISTRY
from rpc_state_indexer.compute.cl_profile import ClProfileModule

DATE = date(2026, 7, 18)
POOL = "0x" + "b0" * 20
ATTEMPT = uuid4()
DIGEST = "d" * 64


class FakeStore:
    database = "test"

    def __init__(self, states, ticks) -> None:
        self._states = states
        self._ticks = ticks
        self.written: list[dict[str, Any]] = []
        self.dedup_tokens: list[str | None] = []

    def query_rows(self, sql: str, parameters: Mapping[str, Any] | None = None):
        if "v_pool_cl_state_published" in sql:
            return list(self._states)
        # tick query is filtered by pool + attempt in the real store; the fake serves all
        return list(self._ticks)

    def insert_rows(self, table, rows, *, deduplication_token=None) -> int:
        materialized = [dict(r) for r in rows]
        self.written.extend(materialized)
        self.dedup_tokens.append(deduplication_token)
        return len(materialized)


def _state():
    return {"pool_address": POOL, "attempt_id": ATTEMPT, "result_digest": DIGEST}


def _ticks(pairs):
    # pairs: list of (tick, liquidity_net)
    return [{"tick": t, "liquidity_net": n} for t, n in pairs]


def test_two_boundary_position_reconstructs_one_segment() -> None:
    L = 10**18
    store = FakeStore([_state()], _ticks([(-4096, L), (4096, -L)]))
    written = ClProfileModule().compute(store, chain_id=100, snapshot_date=DATE)

    assert written == 1
    seg = store.written[0]
    assert seg["tick_lower"] == -4096
    assert seg["tick_upper"] == 4096
    assert seg["active_liquidity"] == L  # cumulative net at the lower boundary
    assert seg["source_attempt_id"] == ATTEMPT
    assert seg["source_result_digest"] == DIGEST
    assert store.dedup_tokens == [f"profile:{ATTEMPT}"]


def test_multi_position_prefix_sums_and_zero_edges() -> None:
    # Three stacked positions; net sums to zero. Active L per segment is the running prefix.
    ticks = _ticks([(-30, 100), (-10, 50), (10, -50), (30, -100)])
    store = FakeStore([_state()], ticks)
    ClProfileModule().compute(store, chain_id=100, snapshot_date=DATE)

    segments = [(r["tick_lower"], r["tick_upper"], r["active_liquidity"]) for r in store.written]
    assert segments == [
        (-30, -10, 100),   # prefix after -30
        (-10, 10, 150),    # + 50
        (10, 30, 100),     # - 50
    ]
    # highest prefix (after +30) is 0 -> the unbounded top region is omitted (only 3 rows)
    assert len(segments) == 3


def test_state_only_pool_with_no_ticks_writes_nothing() -> None:
    store = FakeStore([_state()], _ticks([]))
    assert ClProfileModule().compute(store, chain_id=100, snapshot_date=DATE) == 0
    assert store.written == []


def test_negative_reconstruction_fails_closed() -> None:
    # A malformed tick set whose prefix goes negative must raise, never write a bad profile.
    store = FakeStore([_state()], _ticks([(-10, -5), (10, 5)]))
    with pytest.raises(ValueError, match="negative"):
        ClProfileModule().compute(store, chain_id=100, snapshot_date=DATE)


def test_determinism_same_input_same_data_rows() -> None:
    ticks = _ticks([(-30, 100), (-10, 50), (10, -50), (30, -100)])
    first = FakeStore([_state()], ticks)
    second = FakeStore([_state()], ticks)
    ClProfileModule().compute(first, chain_id=100, snapshot_date=DATE)
    ClProfileModule().compute(second, chain_id=100, snapshot_date=DATE)
    assert first.written == second.written  # no timestamps in the Python payload


def test_module_is_registered_and_declares_sources() -> None:
    module = ClProfileModule()
    assert module.name == "cl_profile"
    assert module.output_table == "pool_liquidity_profile"
    assert "v_pool_tick_liquidity_published" in module.sources()
    assert any(m.name == "cl_profile" for m in REGISTRY)
