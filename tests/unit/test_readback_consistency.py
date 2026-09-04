"""Read-back queries must request read-after-write consistency.

Under concurrent targets an attempt's rows are inserted through one per-thread
client and read back through another, i.e. possibly a different replica; without
select_sequential_consistency the digest check blocked ~43% of good publications.
"""

from datetime import date
from typing import Any
from uuid import uuid4

from rpc_state_indexer.storage.repositories import AttemptScope, ClickHouseRepository


class CapturingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def query(self, sql: str, **kwargs: Any) -> Any:
        self.calls.append({"sql": sql, **kwargs})
        # readback_cl_digest refuses an empty state read-back by design; every other
        # read-back digests whatever rows come back, including none.
        rows: list[dict[str, Any]] = []
        if "pool_cl_state" in sql:
            rows = [
                {
                    "pool_address": "0x" + "ab" * 20,
                    "sqrt_price_x96": 0,
                    "current_tick": 0,
                    "liquidity": 0,
                    "fee_growth_global_0_x128": 0,
                    "fee_growth_global_1_x128": 0,
                    "tick_spacing": 1,
                    "fee": 0,
                    "tick_count": 0,
                }
            ]

        class Result:
            result_rows = [[0]]  # terminal_error_count reads count() positionally

            def named_results(self) -> list[dict[str, Any]]:
                return rows

        return Result()


SCOPE = AttemptScope(
    chain_id=100,
    job_name="j",
    target_address="0xabc",
    snapshot_date=date(2026, 8, 1),
    attempt_id=uuid4(),
)


def test_every_readback_asks_for_sequential_consistency() -> None:
    client = CapturingClient()
    repository = ClickHouseRepository(client, "db")

    repository.terminal_error_count(SCOPE)
    repository.readback_universe_digest(SCOPE)
    repository.readback_token_digest(SCOPE)
    repository.readback_pool_digest(SCOPE)
    repository.readback_cl_digest(SCOPE)
    repository.published_target_addresses(
        chain_id=100, job_name="j", target_kind="token", snapshot_date=date(2026, 8, 1)
    )

    assert len(client.calls) == 8  # token and cl read-backs issue two queries each
    for call in client.calls:
        assert call.get("settings") == {"select_sequential_consistency": 1}, call["sql"][:60]


def test_plain_queries_do_not_carry_the_setting() -> None:
    client = CapturingClient()
    repository = ClickHouseRepository(client, "db")

    repository.canonical_anchor(100, date(2026, 8, 1))
    repository.publication_exists(
        chain_id=100,
        job_name="j",
        target_kind="token",
        target_address="0xabc",
        snapshot_date=date(2026, 8, 1),
    )

    assert all("settings" not in call for call in client.calls)
