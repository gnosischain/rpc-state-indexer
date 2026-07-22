from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest

from rpc_state_indexer.collectors.models import (
    CollectionBatchEvidence,
    CollectionError,
    TokenCollectionResult,
)
from rpc_state_indexer.config.loader import load_catalog
from rpc_state_indexer.core.census import CensusRunner
from rpc_state_indexer.domain import (
    BalanceRow,
    BlockRef,
    ExecutorKind,
    FrozenUniverse,
    IntegrityResult,
    ObservationStatus,
    ScalarRow,
)
from rpc_state_indexer.errors import PublicationBlocked
from rpc_state_indexer.execution.base import VerificationEvidence
from rpc_state_indexer.storage.digests import (
    BalanceDigestRow,
    ScalarDigestRow,
    digest_token_observations,
    digest_universe,
)

ROOT = Path(__file__).parents[2]
HOLDER = "0x" + "11" * 20
ANCHOR = BlockRef(47_000_000, "0x" + "aa" * 32, "0x" + "bb" * 32, 1)


class FakeStore:
    database = "test"

    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []
        self.members: list[dict[str, Any]] = []
        self.balances: list[dict[str, Any]] = []
        self.scalars: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.publications: list[dict[str, Any]] = []

    def insert_attempt_state(self, row: Mapping[str, Any]) -> int:
        self.attempts.append(dict(row))
        return 1

    def insert_universe_members(
        self, rows: list[dict[str, Any]], **_: Any
    ) -> int:
        self.members.extend(rows)
        return len(rows)

    def insert_token_balances(
        self, rows: list[dict[str, Any]], **_: Any
    ) -> int:
        self.balances.extend(rows)
        return len(rows)

    def insert_token_scalars(
        self, rows: list[dict[str, Any]], **_: Any
    ) -> int:
        self.scalars.extend(rows)
        return len(rows)

    def insert_terminal_errors(self, rows: list[dict[str, Any]]) -> int:
        self.errors.extend(rows)
        return len(rows)

    def append_publication(self, row: Mapping[str, Any]) -> int:
        self.publications.append(dict(row))
        return 1

    def terminal_error_count(self, attempt_id: object) -> int:
        return sum(row["attempt_id"] == attempt_id for row in self.errors)

    def readback_universe_digest(self, attempt_id: object) -> str:
        return digest_universe(
            (
                str(row["holder_address"]),
                cast(list[str], row["member_sources"]),
            )
            for row in self.members
            if row["attempt_id"] == attempt_id
        )

    def readback_token_digest(self, attempt_id: object) -> str:
        return digest_token_observations(
            (
                BalanceDigestRow(
                    str(row["holder_address"]),
                    int(row["balance_raw"]),
                    cast(int | None, row["scaled_balance_raw"]),
                    str(row["value_kind"]),
                )
                for row in self.balances
                if row["attempt_id"] == attempt_id
            ),
            (
                ScalarDigestRow(str(row["scalar_name"]), int(row["scalar_raw"]))
                for row in self.scalars
                if row["attempt_id"] == attempt_id
            ),
        )


class FakeUniverseResolver:
    def resolve(self, *_: Any, **__: Any) -> FrozenUniverse:
        sources = {HOLDER: ("test",)}
        return FrozenUniverse((HOLDER,), sources, digest_universe(sources))


class FakeCodeVerifier:
    async def verify(self, *_: Any, **__: Any) -> None:
        return None


class FakeCollector:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def collect(self, **kwargs: Any) -> TokenCollectionResult:
        token = kwargs["token"]
        universe = kwargs["universe"]
        mode = kwargs["integrity_mode"]
        evidence = VerificationEvidence(
            ExecutorKind.MULTICALL3,
            "eip1898",
            ANCHOR.block_hash,
            ("provider-a",),
            "c" * 64,
            True,
        )
        errors = (
            CollectionError(
                HOLDER,
                "balanceOf",
                ObservationStatus.RPC_ERROR,
                0,
                "failed",
            ),
        ) if self.fail else ()
        balances = () if self.fail else (BalanceRow(HOLDER, 0),)
        scalars = (ScalarRow("totalSupply", 0),)
        return TokenCollectionResult(
            token.address,
            universe.universe_hash,
            mode,
            2,
            balances,
            scalars,
            errors,
            (CollectionBatchEvidence(0, 2, evidence),),
            (
                IntegrityResult(
                    not self.fail,
                    "observations_complete",
                    1 if self.fail else 2,
                    2,
                ),
                IntegrityResult.complete("scoped_coverage_no_supply_invariant"),
            ),
        )


def runner(store: FakeStore, collector: FakeCollector) -> tuple[CensusRunner, Any, Any]:
    catalog = load_catalog(ROOT / "config", "gnosis")
    job = catalog.jobs["daily_treasury"]
    token = catalog.tokens["0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"]
    subject = CensusRunner(
        catalog=catalog,
        store=cast(Any, store),
        universe_resolver=cast(Any, FakeUniverseResolver()),
        erc20_collector=cast(Any, collector),
        atoken_collector=cast(Any, collector),
        pool_collector=cast(Any, collector),
        code_verifier=cast(Any, FakeCodeVerifier()),
    )
    return subject, job, token


@pytest.mark.asyncio
async def test_dense_observed_zero_can_be_published() -> None:
    store = FakeStore()
    subject, job, token = runner(store, FakeCollector())

    await subject.run_token(job, token, date(2026, 7, 18), ANCHOR)

    assert len(store.publications) == 1
    assert store.balances[0]["balance_raw"] == 0
    verified = store.attempts[-1]
    assert verified["status"] == "verified"
    # Batch verification evidence is folded onto the attempt row (was census_batches).
    folded = json.loads(verified["batches_json"])
    assert isinstance(folded, list) and len(folded) == verified["batches_total"]
    assert all("verified" in batch for batch in folded)


@pytest.mark.asyncio
async def test_terminal_failure_blocks_publication_and_never_writes_zero() -> None:
    store = FakeStore()
    subject, job, token = runner(store, FakeCollector(fail=True))

    with pytest.raises(PublicationBlocked):
        await subject.run_token(job, token, date(2026, 7, 18), ANCHOR)

    assert store.publications == []
    assert store.balances == []
    assert len(store.errors) == 1
    assert store.attempts[-1]["status"] == "failed"
