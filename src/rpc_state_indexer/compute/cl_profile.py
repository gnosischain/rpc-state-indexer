"""Reconstruct the concentrated-liquidity profile from published tick primitives.

Between two adjacent initialized ticks the active liquidity is constant and equals the
running sum of ``liquidity_net`` over all initialized ticks at or below the lower bound
(``liquidity_net`` is the change applied when the tick is crossed upward). Walking the
sorted ticks yields one ``[tick_lower, tick_upper)`` segment per gap. Below the lowest and
above the highest initialized tick the active liquidity is zero (the ΣliquidityNet==0
invariant), so those unbounded regions are omitted.

Pure DB-in/DB-out, deterministic, RPC-free. Each derived row carries provenance
(``source_attempt_id`` + ``source_result_digest``) back to the verified CL publication.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from .base import ComputeStore


def _digest_str(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("ascii") if isinstance(value, bytes) else str(value)


class ClProfileModule:
    name = "cl_profile"
    output_table = "pool_liquidity_profile"

    def sources(self) -> tuple[str, ...]:
        return ("v_pool_cl_state_published", "v_pool_tick_liquidity_published")

    def compute(
        self, store: ComputeStore, *, chain_id: int, snapshot_date: date
    ) -> int:
        states = store.query_rows(
            f"""
            SELECT pool_address, attempt_id, result_digest
            FROM {store.database}.v_pool_cl_state_published
            WHERE chain_id = {{chain_id:UInt64}}
              AND snapshot_date = {{snapshot_date:Date}}
            """,
            {"chain_id": chain_id, "snapshot_date": snapshot_date},
        )
        written = 0
        for state in states:
            ticks = store.query_rows(
                f"""
                SELECT tick, liquidity_net
                FROM {store.database}.v_pool_tick_liquidity_published
                WHERE chain_id = {{chain_id:UInt64}}
                  AND snapshot_date = {{snapshot_date:Date}}
                  AND pool_address = {{pool:String}}
                  AND attempt_id = {{attempt:UUID}}
                ORDER BY tick
                """,
                {
                    "chain_id": chain_id,
                    "snapshot_date": snapshot_date,
                    "pool": state["pool_address"],
                    "attempt": state["attempt_id"],
                },
            )
            rows = self._segments(chain_id, snapshot_date, state, ticks)
            if rows:
                store.insert_rows(
                    self.output_table,
                    rows,
                    deduplication_token=f"profile:{state['attempt_id']}",
                )
            written += len(rows)
        return written

    def _segments(
        self,
        chain_id: int,
        snapshot_date: date,
        state: Mapping[str, Any],
        ticks: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        pool = str(state["pool_address"])
        digest = _digest_str(state["result_digest"])
        rows: list[dict[str, Any]] = []
        cumulative = 0
        for lower, upper in zip(ticks, ticks[1:], strict=False):
            cumulative += int(lower["liquidity_net"])
            if cumulative < 0:
                # Source was published (ΣliquidityNet invariants held), so this cannot
                # happen; fail closed rather than write a corrupt profile.
                raise ValueError(
                    f"reconstructed liquidity went negative for {pool} at tick "
                    f"{lower['tick']}: {cumulative}"
                )
            rows.append(
                {
                    "chain_id": chain_id,
                    "pool_address": pool,
                    "snapshot_date": snapshot_date,
                    "tick_lower": int(lower["tick"]),
                    "tick_upper": int(upper["tick"]),
                    "active_liquidity": cumulative,
                    "source_attempt_id": state["attempt_id"],
                    "source_result_digest": digest,
                }
            )
        return rows


__all__ = ["ClProfileModule"]
