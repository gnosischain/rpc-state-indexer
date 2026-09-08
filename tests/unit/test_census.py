from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

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
    IntegrityMode,
    IntegrityResult,
    ObservationStatus,
    ScalarRow,
)
from rpc_state_indexer.errors import PublicationBlocked
from rpc_state_indexer.execution.base import VerificationEvidence
from rpc_state_indexer.observability.metrics import SUPPLY_RESIDUAL_PPM
from rpc_state_indexer.storage.digests import (
    BalanceDigestRow,
    ScalarDigestRow,
    digest_token_observations,
    digest_universe,
)
from rpc_state_indexer.storage.repositories import AttemptScope, ClickHouseRepository

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

    def terminal_error_count(self, scope: AttemptScope, *, consistent: bool = False) -> int:
        return sum(row["attempt_id"] == scope.attempt_id for row in self.errors)

    def readback_universe_digest(self, scope: AttemptScope, *, consistent: bool = False) -> str:
        return digest_universe(
            (
                str(row["holder_address"]),
                cast(list[str], row["member_sources"]),
            )
            for row in self.members
            if row["attempt_id"] == scope.attempt_id
        )

    def readback_token_digest(self, scope: AttemptScope, *, consistent: bool = False) -> str:
        return digest_token_observations(
            (
                BalanceDigestRow(
                    str(row["holder_address"]),
                    int(row["balance_raw"]),
                    cast(int | None, row["scaled_balance_raw"]),
                    str(row["value_kind"]),
                )
                for row in self.balances
                if row["attempt_id"] == scope.attempt_id
            ),
            (
                ScalarDigestRow(str(row["scalar_name"]), int(row["scalar_raw"]))
                for row in self.scalars
                if row["attempt_id"] == scope.attempt_id
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
    def __init__(
        self,
        *,
        fail: bool = False,
        balance: int = 0,
        holders: tuple[str, ...] = (HOLDER,),
    ) -> None:
        self.fail = fail
        self.balance = balance
        self.holders = holders

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
        balances = (
            ()
            if self.fail
            else tuple(BalanceRow(holder, self.balance) for holder in self.holders)
        )
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


def runner(
    store: FakeStore,
    collector: FakeCollector,
    job_name: str = "daily_treasury",
) -> tuple[CensusRunner, Any, Any]:
    catalog = load_catalog(ROOT / "config", "gnosis")
    job = catalog.jobs[job_name]
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


def _residual_ppm_for(symbol: str) -> float | None:
    """Current value of the supply-residual gauge for a token symbol, or None if unset."""
    for metric in SUPPLY_RESIDUAL_PPM.collect():
        for sample in metric.samples:
            if sample.labels.get("token") == symbol:
                return sample.value
    return None


def _clear_residual(symbol: str) -> None:
    try:
        SUPPLY_RESIDUAL_PPM.remove(symbol)
    except KeyError:
        pass


@pytest.mark.asyncio
async def test_supply_residual_gauge_skips_scoped_jobs() -> None:
    # daily_treasury is a `scoped` job: it reads totalSupply but only sums a subset of holders,
    # so its holder-sum-vs-supply residual is meaningless (a spurious ~100%). It must NOT touch
    # the token-labelled gauge, or it would overwrite the real full_supply reading for that token.
    store = FakeStore()
    subject, job, token = runner(store, FakeCollector())  # daily_treasury -> scoped
    _clear_residual(token.symbol)

    await subject.run_token(job, token, date(2026, 7, 18), ANCHOR)

    assert len(store.publications) == 1
    assert _residual_ppm_for(token.symbol) is None


@pytest.mark.asyncio
async def test_supply_residual_gauge_set_for_full_supply_jobs() -> None:
    # daily_curated_balances is a `full_supply` job: observed_sum is the full holder sweep, so the
    # residual vs totalSupply IS the reconciliation and must be published to the gauge.
    store = FakeStore()
    subject, job, token = runner(store, FakeCollector(), job_name="daily_curated_balances")
    _clear_residual(token.symbol)

    await subject.run_token(job, token, date(2026, 7, 18), ANCHOR)

    assert len(store.publications) == 1
    assert _residual_ppm_for(token.symbol) == 0.0


@pytest.mark.asyncio
async def test_observed_sum_overflowing_uint256_blocks_publication() -> None:
    # A discovered spam token can mint ~uint256 max to several treasury wallets. Each
    # balance is a valid uint256 return, but their sum is not — the UInt256 column would
    # raise an opaque serialization error *after* the attempt was marked verified.
    store = FakeStore()
    # Two wallets each holding 2**255 — every read is a valid uint256, the sum is not.
    subject, job, token = runner(
        store,
        FakeCollector(balance=1 << 255, holders=(HOLDER, "0x" + "22" * 20)),
    )

    with pytest.raises(PublicationBlocked) as excinfo:
        await subject.run_token(job, token, date(2026, 7, 18), ANCHOR)

    assert "observed_sum_overflow" in str(excinfo.value)
    assert store.publications == []
    # Recorded as failed so the discovered-target quarantine can retire it.
    assert store.attempts[-1]["status"] == "failed"


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


class ThreadRecordingStore(FakeStore):
    """Records which thread each persistence/read-back call ran on."""

    def __init__(self) -> None:
        super().__init__()
        self.threads: dict[str, list[int]] = {}

    def _mark(self, name: str) -> None:
        self.threads.setdefault(name, []).append(threading.get_ident())

    def insert_attempt_state(self, row: Mapping[str, Any]) -> int:
        self._mark("attempt_state")
        return super().insert_attempt_state(row)

    def insert_universe_members(self, rows: list[dict[str, Any]], **kwargs: Any) -> int:
        self._mark("universe")
        return super().insert_universe_members(rows, **kwargs)

    def insert_token_balances(self, rows: list[dict[str, Any]], **kwargs: Any) -> int:
        self._mark("balances")
        return super().insert_token_balances(rows, **kwargs)

    def insert_token_scalars(self, rows: list[dict[str, Any]], **kwargs: Any) -> int:
        self._mark("scalars")
        return super().insert_token_scalars(rows, **kwargs)

    def readback_universe_digest(self, scope: AttemptScope, *, consistent: bool = False) -> str:
        self._mark("readback_universe")
        return super().readback_universe_digest(scope, consistent=consistent)

    def readback_token_digest(self, scope: AttemptScope, *, consistent: bool = False) -> str:
        self._mark("readback_token")
        return super().readback_token_digest(scope, consistent=consistent)

    def append_publication(self, row: Mapping[str, Any]) -> int:
        self._mark("publication")
        return super().append_publication(row)


@pytest.mark.asyncio
async def test_attempt_rows_and_their_readbacks_share_one_thread() -> None:
    # Read-your-writes across ClickHouse Cloud replicas is guaranteed only when the
    # inserts and the read-backs go through the same client, i.e. the same thread.
    # A split across worker threads blocked ~43% of publications in production.
    store = ThreadRecordingStore()
    subject, job, token = runner(store, FakeCollector())

    await subject.run_token(job, token, date(2026, 7, 18), ANCHOR)

    assert len(store.publications) == 1
    bound = {
        name: store.threads[name]
        for name in (
            "universe",
            "balances",
            "scalars",
            "readback_universe",
            "readback_token",
            "publication",
        )
    }
    idents = {ident for calls in bound.values() for ident in calls}
    assert len(idents) == 1, bound
    # ...and none of it ran on the event-loop thread.
    assert threading.get_ident() not in idents


class FlakyReadbackStore(FakeStore):
    """First (fast) read-back returns a stale digest, like a read that landed on the
    other replica before replication; the consistent re-read returns the truth."""

    def __init__(self, *, always_stale: bool = False) -> None:
        super().__init__()
        self.always_stale = always_stale
        self.consistent_reads: list[str] = []

    def readback_token_digest(self, scope: AttemptScope, *, consistent: bool = False) -> str:
        if consistent:
            self.consistent_reads.append("token")
        if self.always_stale or not consistent:
            return "stale"
        return super().readback_token_digest(scope)

    def readback_universe_digest(self, scope: AttemptScope, *, consistent: bool = False) -> str:
        if consistent:
            self.consistent_reads.append("universe")
        if self.always_stale or not consistent:
            return "stale"
        return super().readback_universe_digest(scope)


@pytest.mark.asyncio
async def test_stale_fast_readback_is_settled_by_one_consistent_reread() -> None:
    store = FlakyReadbackStore()
    subject, job, token = runner(store, FakeCollector())

    await subject.run_token(job, token, date(2026, 7, 18), ANCHOR)

    assert len(store.publications) == 1  # published, not blocked
    assert sorted(store.consistent_reads) == ["token", "universe"]  # exactly one re-read each


@pytest.mark.asyncio
async def test_persistent_readback_mismatch_still_blocks() -> None:
    store = FlakyReadbackStore(always_stale=True)
    subject, job, token = runner(store, FakeCollector())

    with pytest.raises(PublicationBlocked):
        await subject.run_token(job, token, date(2026, 7, 18), ANCHOR)

    assert store.publications == []
    assert store.attempts[-1]["status"] == "failed"


class CapturingClient:
    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []

    def insert(self, table: str, data: Any, *, column_names: Any, settings: dict[str, Any]) -> None:
        self.inserts.append({"table": table, "rows": len(data), "settings": settings})


class ChunkRecordingStore(FakeStore):
    """Routes balance inserts through the real repository so the dedup token is
    the one production would send, and records each store call's rows."""

    def __init__(self) -> None:
        super().__init__()
        self.client = CapturingClient()
        self.repository = ClickHouseRepository(cast(Any, self.client), "db")
        self.balance_calls: list[list[dict[str, Any]]] = []

    def insert_token_balances(self, rows: list[dict[str, Any]], **kwargs: Any) -> int:
        self.balance_calls.append(list(rows))
        self.repository.insert_token_balances(rows, **kwargs)
        return super().insert_token_balances(rows, **kwargs)


def _large_token_result(
    token_address: str, universe_hash: str, holders: int
) -> TokenCollectionResult:
    evidence = VerificationEvidence(
        ExecutorKind.MULTICALL3, "eip1898", ANCHOR.block_hash, ("provider-a",), "c" * 64, True
    )
    per_batch = 1_000
    batch_count = -(-holders // per_batch)
    balances = tuple(
        BalanceRow(f"0x{index:040x}", index, batch_sequence=index // per_batch)
        for index in range(holders)
    )
    batches = tuple(
        CollectionBatchEvidence(sequence, per_batch, evidence) for sequence in range(batch_count)
    )
    return TokenCollectionResult(
        token_address,
        universe_hash,
        IntegrityMode.SCOPED,
        holders + 1,
        balances,
        (ScalarRow("totalSupply", 0),),
        (),
        batches,
        (IntegrityResult.complete("observations_complete"),),
    )


def test_balances_are_inserted_in_chunks_with_distinct_dedup_tokens() -> None:
    # 450k rows -> 200k + 200k + 50k: three store calls, not one per multicall batch.
    store = ChunkRecordingStore()
    subject, job, token = runner(store, FakeCollector())
    result = _large_token_result(token.address, "u" * 64, 450_000)
    attempt = uuid4()

    subject._persist_token_result(attempt, job, token, date(2026, 7, 18), result)

    assert [len(rows) for rows in store.balance_calls] == [200_000, 200_000, 50_000]
    balance_inserts = [i for i in store.client.inserts if i["table"] == "db.token_balances"]
    tokens = [i["settings"]["insert_deduplication_token"] for i in balance_inserts]
    assert tokens == [f"{attempt}:balances:{index}" for index in range(3)]
    assert len(set(tokens)) == 3
    for insert in balance_inserts:
        assert insert["settings"]["async_insert"] == 0
    # The union of the chunks is the input, in order, with batch_sequence preserved.
    persisted = [
        (row["holder_address"], row["batch_sequence"], row["probe_source"])
        for rows in store.balance_calls
        for row in rows
    ]
    assert persisted == [
        (row.holder_address, row.batch_sequence, "multicall3") for row in result.balances
    ]
    assert persisted[-1][1] == 449


class FailingChunkStore(FakeStore):
    def insert_token_balances(self, rows: list[dict[str, Any]], **kwargs: Any) -> int:
        raise RuntimeError("chunk insert failed")


@pytest.mark.asyncio
async def test_failed_chunk_insert_marks_attempt_failed_and_publishes_nothing() -> None:
    store = FailingChunkStore()
    subject, job, token = runner(store, FakeCollector())

    with pytest.raises(RuntimeError, match="chunk insert failed"):
        await subject.run_token(job, token, date(2026, 7, 18), ANCHOR)

    assert store.publications == []
    assert store.balances == []
    failed = store.attempts[-1]
    assert failed["status"] == "failed"
    assert failed["error_class"] == "RuntimeError"
