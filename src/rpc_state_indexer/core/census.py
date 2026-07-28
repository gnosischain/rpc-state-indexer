"""Append-only census attempts and fail-closed publication."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID, uuid4

from rpc_state_indexer.collectors import (
    ATokenCollector,
    BalancerPoolCollector,
    ClLiquidityCollector,
    Erc20Collector,
    PoolClCollectionResult,
    PoolCollectionResult,
    PoolReserveCollector,
    TokenCollectionResult,
)
from rpc_state_indexer.collectors.atoken import UINT256_MAX
from rpc_state_indexer.collectors.models import CollectionBatchEvidence, CollectionError
from rpc_state_indexer.config.hashing import canonical_hash, canonical_json
from rpc_state_indexer.config.loader import Catalog
from rpc_state_indexer.config.models import JobConfig, PoolConfig, TokenConfig
from rpc_state_indexer.core.anchors import ResolvedAnchor, assert_anchor_immutable
from rpc_state_indexer.core.universes import UniverseResolver
from rpc_state_indexer.domain import (
    BlockRef,
    ExecutorKind,
    FrozenUniverse,
    IntegrityMode,
)
from rpc_state_indexer.errors import PublicationBlocked
from rpc_state_indexer.execution.code import HistoricalCodeVerifier
from rpc_state_indexer.observability.metrics import (
    CENSUS_CALL_FAILURES,
    CENSUS_CALLS,
    PUBLISH_LAG_DAYS,
    SUPPLY_RESIDUAL_PPM,
    SUSPECT_ZEROS,
)
from rpc_state_indexer.storage.digests import digest_universe
from rpc_state_indexer.storage.repositories import AttemptScope


class CensusStore(Protocol):
    database: str

    def register_configs(self, rows: list[dict[str, Any]]) -> int: ...

    def canonical_anchor(
        self, chain_id: int, snapshot_date: date
    ) -> dict[str, Any] | None: ...

    def insert_anchor(self, row: dict[str, Any]) -> int: ...

    def insert_attempt_state(self, row: dict[str, Any]) -> int: ...

    def insert_universe_members(
        self, rows: list[dict[str, Any]], *, attempt_id: UUID
    ) -> int: ...

    def insert_token_balances(
        self,
        rows: list[dict[str, Any]],
        *,
        attempt_id: UUID,
        batch_sequence: int,
    ) -> int: ...

    def insert_token_scalars(
        self,
        rows: list[dict[str, Any]],
        *,
        attempt_id: UUID,
        batch_sequence: int,
    ) -> int: ...

    def insert_pool_balances(
        self,
        rows: list[dict[str, Any]],
        *,
        attempt_id: UUID,
        batch_sequence: int,
    ) -> int: ...

    def insert_pool_cl_state(
        self, rows: list[dict[str, Any]], *, attempt_id: UUID
    ) -> int: ...

    def insert_pool_ticks(
        self,
        rows: list[dict[str, Any]],
        *,
        attempt_id: UUID,
        batch_sequence: int,
    ) -> int: ...

    def insert_terminal_errors(self, rows: list[dict[str, Any]]) -> int: ...

    def append_publication(self, row: dict[str, Any]) -> int: ...

    def terminal_error_count(self, scope: AttemptScope) -> int: ...

    def readback_universe_digest(self, scope: AttemptScope) -> str: ...

    def readback_token_digest(self, scope: AttemptScope) -> str: ...

    def readback_pool_digest(self, scope: AttemptScope) -> str: ...

    def readback_cl_digest(self, scope: AttemptScope) -> str: ...


def executor_kind_for_anchor(catalog: Catalog, anchor: BlockRef) -> ExecutorKind:
    if anchor.number >= catalog.chain.multicall3.deployment_block:
        return ExecutorKind.MULTICALL3
    return ExecutorKind.LEGACY_RPC_BATCH


class AnchorStoreService:
    """Persist a resolution once and reject any later drift."""

    def __init__(self, store: CensusStore, chain_id: int) -> None:
        self.store = store
        self.chain_id = chain_id

    def persist(self, resolved: ResolvedAnchor) -> BlockRef:
        existing = self.store.canonical_anchor(self.chain_id, resolved.snapshot_date)
        if existing is not None:
            stored = BlockRef(
                number=int(existing["block_number"]),
                block_hash=str(existing["block_hash"]),
                parent_hash=str(existing["parent_hash"]),
                timestamp=int(existing["block_timestamp"].timestamp()),
            )
            assert_anchor_immutable(stored, resolved.anchor)
            return stored

        fingerprint = hashlib.sha256(
            "\x00".join(resolved.endpoint_fingerprints).encode()
        ).hexdigest()
        self.store.insert_anchor(
            {
                "chain_id": self.chain_id,
                "snapshot_date": resolved.snapshot_date,
                "resolution_id": uuid4(),
                "block_number": resolved.anchor.number,
                "block_hash": resolved.anchor.block_hash,
                "parent_hash": resolved.anchor.parent_hash,
                "block_timestamp": datetime.fromtimestamp(
                    resolved.anchor.timestamp, UTC
                ),
                "next_block_number": resolved.next_block.number,
                "next_block_hash": resolved.next_block.block_hash,
                "next_block_timestamp": datetime.fromtimestamp(
                    resolved.next_block.timestamp, UTC
                ),
                "finalized_at_resolution": 1,
                "resolution_source": resolved.finality_source,
                "endpoint_fingerprint": fingerprint,
                "resolved_at": datetime.now(UTC),
            }
        )
        return resolved.anchor


class CatalogRegistrar:
    """Publish the effective entity/job catalog used by eligible views."""

    def __init__(self, store: CensusStore, catalog: Catalog) -> None:
        self.store = store
        self.catalog = catalog

    def register(self) -> None:
        configs: list[dict[str, Any]] = []
        for job in self.catalog.jobs.values():
            if job.target_kind == "tokens":
                configs.extend(
                    self._rows(job, self.catalog.token_targets(job), "token")
                )
            else:
                configs.extend(self._rows(job, self.catalog.pool_targets(job), "pool"))
        self.store.register_configs(configs)

    def register_targets(
        self,
        job: JobConfig,
        targets: Sequence[TokenConfig | PoolConfig],
    ) -> None:
        """Register runtime-resolved targets (discovered selectors) for a job.

        Publications only surface through views when their exact target + config hash is
        registered, so discovered targets must be registered before their first census.
        """

        target_kind = "token" if job.target_kind == "tokens" else "pool"
        rows = self._rows(job, tuple(targets), target_kind)
        if rows:
            self.store.register_configs(rows)

    def _rows(
        self,
        job: JobConfig,
        targets: tuple[TokenConfig | PoolConfig, ...],
        target_kind: str,
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        configs: list[dict[str, Any]] = []
        for target in targets:
            effective = self.catalog.target_effective_config(job, target)
            coverage_candidates = tuple(
                value
                for value in (job.coverage_start, target.date_start)
                if value is not None
            )
            configs.append(
                {
                    "chain_id": self.catalog.chain.chain_id,
                    "job_name": job.name,
                    "target_kind": target_kind,
                    "target_address": target.address,
                    "cadence": job.cadence,
                    "integrity_mode": job.integrity_mode.value,
                    "coverage_start": (
                        max(coverage_candidates) if coverage_candidates else None
                    ),
                    "coverage_end": target.date_end,
                    "config_hash": canonical_hash(effective),
                    "canonical_config_json": canonical_json(effective),
                    "enabled": int(target.enabled),
                    "registered_at": now,
                }
            )
        return configs


class CensusRunner:
    def __init__(
        self,
        *,
        catalog: Catalog,
        store: CensusStore,
        universe_resolver: UniverseResolver,
        erc20_collector: Erc20Collector,
        atoken_collector: ATokenCollector,
        pool_collector: PoolReserveCollector,
        code_verifier: HistoricalCodeVerifier,
        balancer_collector: BalancerPoolCollector | None = None,
        cl_collector: ClLiquidityCollector | None = None,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.universe_resolver = universe_resolver
        self.erc20_collector = erc20_collector
        self.atoken_collector = atoken_collector
        self.pool_collector = pool_collector
        self.balancer_collector = balancer_collector
        self.cl_collector = cl_collector
        self.code_verifier = code_verifier

    async def run_token(
        self,
        job: JobConfig,
        token: TokenConfig,
        snapshot_date: date,
        anchor: BlockRef,
    ) -> UUID:
        if job.universe is None:
            raise ValueError("token census requires a universe selector")
        self._check_date_window(token, snapshot_date)
        universe = self.universe_resolver.resolve(
            job.universe,
            token_address=token.address,
            anchor_block=anchor.number,
        )
        attempt_id = uuid4()
        started = datetime.now(UTC)
        config_hash = self.catalog.target_config_hash(job, token)
        executor_kind = executor_kind_for_anchor(self.catalog, anchor)
        base = self._attempt_base(
            attempt_id,
            job,
            "token",
            token.address,
            snapshot_date,
            anchor,
            config_hash,
            universe,
            executor_kind,
            started,
        )
        self.store.insert_attempt_state({**base, "status": "started"})
        self._persist_universe(
            attempt_id, job, token.address, snapshot_date, universe
        )
        try:
            await self._verify_token_code(token, anchor, executor_kind)
            collector = self.atoken_collector if token.is_atoken else self.erc20_collector
            result = await collector.collect(
                token=token,
                universe=universe,
                anchor=anchor,
                integrity_mode=job.integrity_mode,
            )
            CENSUS_CALLS.labels(job=job.name, token=token.symbol).inc(
                result.expected_calls
            )
            SUSPECT_ZEROS.labels(token=token.symbol).inc(0)
            for error in result.errors:
                CENSUS_CALL_FAILURES.labels(reason=error.status.value).inc()
            self._persist_token_result(
                attempt_id, job, token, snapshot_date, result
            )
            self._publish_token(
                base, attempt_id, job, token, snapshot_date, universe, result
            )
        except BaseException as exc:
            self.store.insert_attempt_state(
                {
                    **base,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "error_message": str(exc)[:4096],
                    "finished_at": datetime.now(UTC),
                }
            )
            raise
        return attempt_id

    async def run_pool(
        self,
        job: JobConfig,
        pool: PoolConfig,
        snapshot_date: date,
        anchor: BlockRef,
    ) -> UUID:
        self._check_date_window(pool, snapshot_date)
        empty_sources: MappingProxyType[str, tuple[str, ...]] = MappingProxyType({})
        universe = FrozenUniverse((), empty_sources, digest_universe(empty_sources))
        attempt_id = uuid4()
        started = datetime.now(UTC)
        config_hash = self.catalog.target_config_hash(job, pool)
        executor_kind = executor_kind_for_anchor(self.catalog, anchor)
        base = self._attempt_base(
            attempt_id,
            job,
            "pool",
            pool.address,
            snapshot_date,
            anchor,
            config_hash,
            universe,
            executor_kind,
            started,
        )
        self.store.insert_attempt_state({**base, "status": "started"})
        try:
            await self.code_verifier.verify(pool.address, anchor)
            for asset in pool.assets:
                await self.code_verifier.verify(asset.token, anchor)
            if executor_kind is ExecutorKind.MULTICALL3:
                await self._verify_multicall_code(anchor)
            if job.integrity_mode is IntegrityMode.CL_LIQUIDITY:
                if self.cl_collector is None:
                    raise ValueError(
                        f"job {job.name} is cl_liquidity but no CL collector is configured"
                    )
                cl_result = await self.cl_collector.collect(
                    pool=pool, anchor=anchor, integrity_mode=job.integrity_mode
                )
                self._persist_pool_cl_result(attempt_id, job, pool, snapshot_date, cl_result)
                self._publish_pool_cl(
                    base, attempt_id, job, pool, snapshot_date, universe, cl_result
                )
                return attempt_id
            if pool.is_balancer:
                if self.balancer_collector is None:
                    raise ValueError(
                        f"pool {pool.address} is {pool.pool_class} but no Balancer "
                        "collector is configured"
                    )
                await self.code_verifier.verify(
                    self.balancer_collector.vault_target(pool), anchor
                )
                result = await self.balancer_collector.collect(
                    pool=pool, anchor=anchor, integrity_mode=job.integrity_mode
                )
            else:
                result = await self.pool_collector.collect(
                    pool=pool, anchor=anchor, integrity_mode=job.integrity_mode
                )
            self._persist_pool_result(
                attempt_id, job, pool, snapshot_date, result
            )
            self._publish_pool(
                base, attempt_id, job, pool, snapshot_date, universe, result
            )
        except BaseException as exc:
            self.store.insert_attempt_state(
                {
                    **base,
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "error_message": str(exc)[:4096],
                    "finished_at": datetime.now(UTC),
                }
            )
            raise
        return attempt_id

    @staticmethod
    def _check_date_window(target: TokenConfig | PoolConfig, value: date) -> None:
        if target.date_start is not None and value < target.date_start:
            raise ValueError("snapshot is before the entity date_start")
        if target.date_end is not None and value >= target.date_end:
            raise ValueError("snapshot is outside the entity date_end")

    async def _verify_token_code(
        self,
        token: TokenConfig,
        anchor: BlockRef,
        executor_kind: ExecutorKind,
    ) -> None:
        await self.code_verifier.verify(token.address, anchor)
        if token.index_source is not None:
            await self.code_verifier.verify(token.index_source.contract, anchor)
        if executor_kind is ExecutorKind.MULTICALL3:
            await self._verify_multicall_code(anchor)

    async def _verify_multicall_code(self, anchor: BlockRef) -> None:
        config = self.catalog.chain.multicall3
        if config.runtime_code_hash is None:
            raise ValueError("Multicall3 runtime code hash is not configured")
        await self.code_verifier.verify(
            config.address,
            anchor,
            expected_code_hash=config.runtime_code_hash,
        )

    def _attempt_base(
        self,
        attempt_id: UUID,
        job: JobConfig,
        target_kind: str,
        target_address: str,
        snapshot_date: date,
        anchor: BlockRef,
        config_hash: str,
        universe: FrozenUniverse,
        executor_kind: ExecutorKind,
        started: datetime,
    ) -> dict[str, Any]:
        return {
            "chain_id": self.catalog.chain.chain_id,
            "job_name": job.name,
            "target_kind": target_kind,
            "target_address": target_address,
            "snapshot_date": snapshot_date,
            "attempt_id": attempt_id,
            "integrity_mode": job.integrity_mode.value,
            "config_hash": config_hash,
            "anchor_block": anchor.number,
            "anchor_hash": anchor.block_hash,
            "executor_kind": executor_kind.value,
            "block_reference_kind": "pending",
            "universe_hash": universe.universe_hash,
            "universe_size": universe.size,
            "batches_total": 0,
            "batches_verified": 0,
            "observations_ok": 0,
            "observations_failed": 0,
            "result_digest": None,
            "error_class": "",
            "error_message": "",
            "started_at": started,
            "heartbeat_at": started,
            "finished_at": None,
        }

    def _persist_universe(
        self,
        attempt_id: UUID,
        job: JobConfig,
        token_address: str,
        snapshot_date: date,
        universe: FrozenUniverse,
    ) -> None:
        now = datetime.now(UTC)
        rows = [
            {
                "chain_id": self.catalog.chain.chain_id,
                "job_name": job.name,
                "target_kind": "token",
                "target_address": token_address,
                "snapshot_date": snapshot_date,
                "attempt_id": attempt_id,
                "holder_address": holder,
                "member_sources": list(universe.sources[holder]),
                "member_ordinal": ordinal,
                "inserted_at": now,
            }
            for ordinal, holder in enumerate(universe.addresses)
        ]
        if rows:
            self.store.insert_universe_members(rows, attempt_id=attempt_id)

    @staticmethod
    def _batches_json(batches: tuple[CollectionBatchEvidence, ...]) -> str:
        """Fold per-batch verification evidence into a canonical JSON blob.

        Stored on the ``census_attempts`` row (``batches_json``) instead of a
        separate ``census_batches`` table. Carries only the per-batch evidence;
        attempt-scoped fields (chain/job/target/anchor) live on the row itself.
        """
        return canonical_json(
            [
                {
                    "batch_sequence": batch.batch_sequence,
                    "executor_kind": batch.evidence.executor_kind.value,
                    "block_reference_kind": batch.evidence.block_reference_kind,
                    "anchor_hash": batch.evidence.anchor_hash,
                    "body_call_count": batch.body_call_count,
                    "provider_groups": list(batch.evidence.provider_groups),
                    "result_digest": batch.evidence.result_digest,
                    "verified": int(batch.evidence.verified),
                }
                for batch in batches
            ]
        )

    def _error_rows(
        self,
        attempt_id: UUID,
        job: JobConfig,
        target_kind: str,
        target_address: str,
        snapshot_date: date,
        errors: tuple[CollectionError, ...],
    ) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        return [
            {
                "chain_id": self.catalog.chain.chain_id,
                "job_name": job.name,
                "target_kind": target_kind,
                "target_address": target_address,
                "snapshot_date": snapshot_date,
                "attempt_id": attempt_id,
                "subject_address": error.subject_address,
                "call_kind": error.call_kind,
                "batch_sequence": error.batch_sequence,
                "error_class": error.status.value,
                "rpc_code": error.rpc_code,
                "return_data": "0x" + error.return_data.hex(),
                "error_message": error.message[:4096],
                "terminal_at": now,
            }
            for error in errors
        ]

    def _persist_token_result(
        self,
        attempt_id: UUID,
        job: JobConfig,
        token: TokenConfig,
        snapshot_date: date,
        result: TokenCollectionResult,
    ) -> None:
        now = datetime.now(UTC)
        balances_by_batch: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for balance_row in result.balances:
            source = result.batches[
                balance_row.batch_sequence
            ].evidence.executor_kind.value
            balances_by_batch[balance_row.batch_sequence].append(
                {
                    "chain_id": self.catalog.chain.chain_id,
                    "job_name": job.name,
                    "token_address": token.address,
                    "snapshot_date": snapshot_date,
                    "attempt_id": attempt_id,
                    "holder_address": balance_row.holder_address,
                    "balance_raw": balance_row.balance_raw,
                    "scaled_balance_raw": balance_row.scaled_balance_raw,
                    "value_kind": balance_row.value_kind,
                    "probe_source": source,
                    "batch_sequence": balance_row.batch_sequence,
                    "observed_at": now,
                }
            )
        for sequence, rows in balances_by_batch.items():
            self.store.insert_token_balances(
                rows, attempt_id=attempt_id, batch_sequence=sequence
            )

        scalars_by_batch: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for scalar_row in result.scalars:
            source = result.batches[
                scalar_row.batch_sequence
            ].evidence.executor_kind.value
            scalars_by_batch[scalar_row.batch_sequence].append(
                {
                    "chain_id": self.catalog.chain.chain_id,
                    "job_name": job.name,
                    "token_address": token.address,
                    "snapshot_date": snapshot_date,
                    "attempt_id": attempt_id,
                    "scalar_name": scalar_row.scalar_name,
                    "scalar_raw": scalar_row.scalar_raw,
                    "probe_source": source,
                    "batch_sequence": scalar_row.batch_sequence,
                    "observed_at": now,
                }
            )
        for sequence, rows in scalars_by_batch.items():
            self.store.insert_token_scalars(
                rows, attempt_id=attempt_id, batch_sequence=sequence
            )
        errors = self._error_rows(
            attempt_id,
            job,
            "token",
            token.address,
            snapshot_date,
            result.errors,
        )
        if errors:
            self.store.insert_terminal_errors(errors)

    def _persist_pool_result(
        self,
        attempt_id: UUID,
        job: JobConfig,
        pool: PoolConfig,
        snapshot_date: date,
        result: PoolCollectionResult,
    ) -> None:
        now = datetime.now(UTC)
        by_batch: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in result.balances:
            source = result.batches[row.batch_sequence].evidence.executor_kind.value
            by_batch[row.batch_sequence].append(
                {
                    "chain_id": self.catalog.chain.chain_id,
                    "job_name": job.name,
                    "pool_address": pool.address,
                    "token_address": row.token_address,
                    "snapshot_date": snapshot_date,
                    "attempt_id": attempt_id,
                    "balance_raw": row.balance_raw,
                    "probe_source": source,
                    "batch_sequence": row.batch_sequence,
                    "observed_at": now,
                }
            )
        for sequence, rows in by_batch.items():
            self.store.insert_pool_balances(
                rows, attempt_id=attempt_id, batch_sequence=sequence
            )
        errors = self._error_rows(
            attempt_id,
            job,
            "pool",
            pool.address,
            snapshot_date,
            result.errors,
        )
        if errors:
            self.store.insert_terminal_errors(errors)

    def _persist_pool_cl_result(
        self,
        attempt_id: UUID,
        job: JobConfig,
        pool: PoolConfig,
        snapshot_date: date,
        result: PoolClCollectionResult,
    ) -> None:
        now = datetime.now(UTC)
        state = result.state
        state_source = result.batches[state.batch_sequence].evidence.executor_kind.value
        self.store.insert_pool_cl_state(
            [
                {
                    "chain_id": self.catalog.chain.chain_id,
                    "job_name": job.name,
                    "pool_address": pool.address,
                    "snapshot_date": snapshot_date,
                    "attempt_id": attempt_id,
                    "pool_class": state.pool_class,
                    "sqrt_price_x96": state.sqrt_price_x96,
                    "current_tick": state.current_tick,
                    "liquidity": state.liquidity,
                    "fee_growth_global_0_x128": state.fee_growth_global_0_x128,
                    "fee_growth_global_1_x128": state.fee_growth_global_1_x128,
                    "tick_spacing": state.tick_spacing,
                    "fee": state.fee,
                    "tick_count": state.tick_count,
                    "probe_source": state_source,
                    "batch_sequence": state.batch_sequence,
                    "observed_at": now,
                }
            ],
            attempt_id=attempt_id,
        )

        by_batch: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for tick in result.ticks:
            source = result.batches[tick.batch_sequence].evidence.executor_kind.value
            by_batch[tick.batch_sequence].append(
                {
                    "chain_id": self.catalog.chain.chain_id,
                    "job_name": job.name,
                    "pool_address": pool.address,
                    "snapshot_date": snapshot_date,
                    "attempt_id": attempt_id,
                    "tick": tick.tick,
                    "liquidity_gross": tick.liquidity_gross,
                    "liquidity_net": tick.liquidity_net,
                    "fee_growth_outside_0_x128": tick.fee_growth_outside_0_x128,
                    "fee_growth_outside_1_x128": tick.fee_growth_outside_1_x128,
                    "probe_source": source,
                    "batch_sequence": tick.batch_sequence,
                    "observed_at": now,
                }
            )
        for sequence, rows in by_batch.items():
            self.store.insert_pool_ticks(
                rows, attempt_id=attempt_id, batch_sequence=sequence
            )

        errors = self._error_rows(
            attempt_id, job, "pool", pool.address, snapshot_date, result.errors
        )
        if errors:
            self.store.insert_terminal_errors(errors)

    def _publication_context(
        self, batches: tuple[CollectionBatchEvidence, ...]
    ) -> tuple[str, str, list[str]]:
        executor_kinds = sorted(
            {batch.evidence.executor_kind.value for batch in batches}
        )
        reference_kinds = sorted(
            {batch.evidence.block_reference_kind for batch in batches}
        )
        providers = sorted(
            {
                group
                for batch in batches
                for group in batch.evidence.provider_groups
            }
        )
        return "+".join(executor_kinds), "+".join(reference_kinds), providers

    @staticmethod
    def _scope(base: dict[str, Any]) -> AttemptScope:
        """Sort-key prefix for this attempt's rows; keeps read-backs point lookups."""

        return AttemptScope(
            chain_id=base["chain_id"],
            job_name=base["job_name"],
            target_address=base["target_address"],
            snapshot_date=base["snapshot_date"],
            attempt_id=base["attempt_id"],
        )

    def _publication_checks(
        self,
        scope: AttemptScope,
        universe: FrozenUniverse,
        result: TokenCollectionResult | PoolCollectionResult | PoolClCollectionResult,
        readback_digest: str,
    ) -> list[str]:
        failed: list[str] = []
        passed: list[str] = ["historical_code_verified"]
        if self.store.terminal_error_count(scope) != 0:
            failed.append("terminal_errors_present")
        else:
            passed.append("zero_terminal_errors")
        if not all(batch.evidence.verified for batch in result.batches):
            failed.append("unverified_batch")
        else:
            passed.append("all_batches_verified")
        for check in result.integrity_checks:
            if check.passed:
                passed.append(check.check)
            else:
                failed.append(check.check)
        if readback_digest != result.result_digest:
            failed.append("observation_readback_digest")
        else:
            passed.append("observation_readback_digest")
        if isinstance(result, TokenCollectionResult):
            if self.store.readback_universe_digest(scope) != universe.universe_hash:
                failed.append("universe_readback_digest")
            else:
                passed.append("universe_readback_digest")
        if failed or not result.verified:
            if not failed:
                failed.append("collector_not_verified")
            raise PublicationBlocked(failed)
        return passed

    def _publish_token(
        self,
        base: dict[str, Any],
        attempt_id: UUID,
        job: JobConfig,
        token: TokenConfig,
        snapshot_date: date,
        universe: FrozenUniverse,
        result: TokenCollectionResult,
    ) -> None:
        scope = self._scope(base)
        readback = self.store.readback_token_digest(scope)
        checks = self._publication_checks(scope, universe, result, readback)
        executor, reference, providers = self._publication_context(result.batches)
        scalar_values = {row.scalar_name: row.scalar_raw for row in result.scalars}
        if job.integrity_mode is IntegrityMode.SCALED_FULL_SUPPLY:
            scaled_values = [row.scaled_balance_raw for row in result.balances]
            if any(value is None for value in scaled_values):
                raise PublicationBlocked(["scaled_balance_missing"])
            observed_sum = sum(
                value for value in scaled_values if value is not None
            )
            reference_supply = scalar_values.get("scaledTotalSupply")
        else:
            observed_sum = sum(row.balance_raw for row in result.balances)
            reference_supply = scalar_values.get("totalSupply")
        if observed_sum > UINT256_MAX:
            # Every individual balance is a valid uint256 return, but their sum is not
            # bounded by uint256 — only an adversarial or broken supply reaches this.
            # Fail closed with a named reason instead of letting the UInt256 column
            # raise an opaque serialization error after the attempt was marked verified.
            raise PublicationBlocked(["observed_sum_overflow"])
        finished = datetime.now(UTC)
        verified_state = {
            **base,
            "status": "verified",
            "block_reference_kind": reference,
            "batches_total": len(result.batches),
            "batches_verified": sum(
                batch.evidence.verified for batch in result.batches
            ),
            "observations_ok": result.successful_calls,
            "observations_failed": len(result.errors),
            "result_digest": result.result_digest,
            "batches_json": self._batches_json(result.batches),
            "heartbeat_at": finished,
            "finished_at": finished,
        }
        self.store.insert_attempt_state(verified_state)
        self.store.append_publication(
            {
                "chain_id": self.catalog.chain.chain_id,
                "job_name": job.name,
                "target_kind": "token",
                "target_address": token.address,
                "snapshot_date": snapshot_date,
                "publication_id": uuid4(),
                "attempt_id": attempt_id,
                "executor_kind": executor,
                "block_reference_kind": reference,
                "integrity_mode": job.integrity_mode.value,
                "config_hash": base["config_hash"],
                "anchor_block": base["anchor_block"],
                "anchor_hash": base["anchor_hash"],
                "universe_hash": universe.universe_hash,
                "universe_size": universe.size,
                "result_digest": result.result_digest,
                "observed_sum_raw": observed_sum,
                "reference_supply_raw": reference_supply,
                "batches_total": len(result.batches),
                "observations_total": result.successful_calls,
                "provider_groups": providers,
                "checks_passed": checks,
                "published_at": finished,
            }
        )
        # The holder-sum vs totalSupply reconciliation is only meaningful for jobs that sweep the
        # FULL holder universe (full_supply / scaled_full_supply). Scoped jobs (treasury, supply
        # probe) read totalSupply as a scalar but only sum a subset, so their "residual" is a
        # spurious ~100%. Emitting it here would (a) trip the supply-residual alert falsely and
        # (b) collide on the token-only gauge label with the real full_supply publication, whose
        # value would then be overwritten depending on publish order. Gate to the reconciling modes.
        if reference_supply is not None and job.integrity_mode in (
            IntegrityMode.FULL_SUPPLY,
            IntegrityMode.SCALED_FULL_SUPPLY,
        ):
            denominator = max(1, reference_supply)
            residual_ppm = abs(observed_sum - reference_supply) * 1_000_000 / denominator
            SUPPLY_RESIDUAL_PPM.labels(token=token.symbol).set(residual_ppm)
        lag = max(0, (datetime.now(UTC).date() - snapshot_date).days)
        PUBLISH_LAG_DAYS.labels(job=job.name, token=token.symbol).set(lag)

    def _publish_pool(
        self,
        base: dict[str, Any],
        attempt_id: UUID,
        job: JobConfig,
        pool: PoolConfig,
        snapshot_date: date,
        universe: FrozenUniverse,
        result: PoolCollectionResult,
    ) -> None:
        scope = self._scope(base)
        readback = self.store.readback_pool_digest(scope)
        checks = self._publication_checks(scope, universe, result, readback)
        executor, reference, providers = self._publication_context(result.batches)
        finished = datetime.now(UTC)
        self.store.insert_attempt_state(
            {
                **base,
                "status": "verified",
                "block_reference_kind": reference,
                "batches_total": len(result.batches),
                "batches_verified": sum(
                    batch.evidence.verified for batch in result.batches
                ),
                "observations_ok": result.successful_calls,
                "observations_failed": len(result.errors),
                "result_digest": result.result_digest,
                "batches_json": self._batches_json(result.batches),
                "heartbeat_at": finished,
                "finished_at": finished,
            }
        )
        self.store.append_publication(
            {
                "chain_id": self.catalog.chain.chain_id,
                "job_name": job.name,
                "target_kind": "pool",
                "target_address": pool.address,
                "snapshot_date": snapshot_date,
                "publication_id": uuid4(),
                "attempt_id": attempt_id,
                "executor_kind": executor,
                "block_reference_kind": reference,
                "integrity_mode": job.integrity_mode.value,
                "config_hash": base["config_hash"],
                "anchor_block": base["anchor_block"],
                "anchor_hash": base["anchor_hash"],
                "universe_hash": universe.universe_hash,
                "universe_size": 0,
                "result_digest": result.result_digest,
                "observed_sum_raw": None,
                "reference_supply_raw": None,
                "batches_total": len(result.batches),
                "observations_total": result.successful_calls,
                "provider_groups": providers,
                "checks_passed": checks,
                "published_at": finished,
            }
        )

    def _publish_pool_cl(
        self,
        base: dict[str, Any],
        attempt_id: UUID,
        job: JobConfig,
        pool: PoolConfig,
        snapshot_date: date,
        universe: FrozenUniverse,
        result: PoolClCollectionResult,
    ) -> None:
        scope = self._scope(base)
        readback = self.store.readback_cl_digest(scope)
        checks = self._publication_checks(scope, universe, result, readback)
        executor, reference, providers = self._publication_context(result.batches)
        finished = datetime.now(UTC)
        self.store.insert_attempt_state(
            {
                **base,
                "status": "verified",
                "block_reference_kind": reference,
                "batches_total": len(result.batches),
                "batches_verified": sum(
                    batch.evidence.verified for batch in result.batches
                ),
                "observations_ok": result.successful_calls,
                "observations_failed": len(result.errors),
                "result_digest": result.result_digest,
                "batches_json": self._batches_json(result.batches),
                "heartbeat_at": finished,
                "finished_at": finished,
            }
        )
        self.store.append_publication(
            {
                "chain_id": self.catalog.chain.chain_id,
                "job_name": job.name,
                "target_kind": "pool",
                "target_address": pool.address,
                "snapshot_date": snapshot_date,
                "publication_id": uuid4(),
                "attempt_id": attempt_id,
                "executor_kind": executor,
                "block_reference_kind": reference,
                "integrity_mode": job.integrity_mode.value,
                "config_hash": base["config_hash"],
                "anchor_block": base["anchor_block"],
                "anchor_hash": base["anchor_hash"],
                "universe_hash": universe.universe_hash,
                "universe_size": 0,
                "result_digest": result.result_digest,
                "observed_sum_raw": None,
                "reference_supply_raw": None,
                "batches_total": len(result.batches),
                "observations_total": result.successful_calls,
                "provider_groups": providers,
                "checks_passed": checks,
                "published_at": finished,
            }
        )
