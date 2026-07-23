"""High-level operations invoked by the CLI and daemon."""

from __future__ import annotations

import asyncio
import calendar
import json
import socket
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from rpc_state_indexer.collectors import (
    ATokenCollector,
    BalancerPoolCollector,
    ClLiquidityCollector,
    Erc20Collector,
    PoolReserveCollector,
)
from rpc_state_indexer.compute import REGISTRY as COMPUTE_REGISTRY
from rpc_state_indexer.config.loader import Catalog
from rpc_state_indexer.config.models import (
    JobConfig,
    PoolConfig,
    TokenConfig,
    UniverseConfig,
)
from rpc_state_indexer.config.validation import validate_runtime_catalog
from rpc_state_indexer.core.anchors import AnchorResolver
from rpc_state_indexer.core.census import (
    AnchorStoreService,
    CatalogRegistrar,
    CensusRunner,
)
from rpc_state_indexer.core.discovery_service import DiscoveryService
from rpc_state_indexer.core.universes import (
    ClickHouseHolderUniverseRepository,
    UniverseResolver,
)
from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.evm.calldata import TOTAL_SUPPLY_SELECTOR
from rpc_state_indexer.evm.events import AbiRegistry
from rpc_state_indexer.execution.base import (
    ContractCall,
    VerificationEvidence,
    VerifiedBatchResult,
)
from rpc_state_indexer.observability.health import HealthServer, start_health_server
from rpc_state_indexer.observability.metrics import (
    CENSUS_PUBLICATIONS,
    COMPUTE_ROWS,
    DAEMON_CYCLES,
    DAEMON_JOB_FAILURES,
    ENDPOINT_HEALTHY,
)
from rpc_state_indexer.rpc.capabilities import probe_endpoint_capabilities
from rpc_state_indexer.runtime import (
    RpcRuntime,
    build_catalog,
    build_repository,
    build_rpc_runtime,
    earliest_archive_probe_token,
)
from rpc_state_indexer.settings import RuntimeSettings
from rpc_state_indexer.storage.repositories import ClickHouseRepository


class ServiceError(RuntimeError):
    """A requested operation could not safely complete."""


class JobRunError(ServiceError):
    def __init__(self, failures: list[str]) -> None:
        self.failures = failures
        super().__init__("; ".join(failures))


@runtime_checkable
class BatchSizedExecutor(Protocol):
    batch_size: int

    async def execute(
        self,
        calls: Sequence[ContractCall],
        anchor: BlockRef,
    ) -> list[VerifiedBatchResult]: ...


@dataclass(frozen=True, slots=True)
class BenchmarkOutcome:
    max_batch_size: int
    recommended_batch_size: int
    timings_seconds: tuple[float, ...]
    evidence: VerificationEvidence


async def benchmark_single_batch_ceiling(
    executor: BatchSizedExecutor,
    *,
    token_address: str,
    anchor: BlockRef,
    max_size: int,
) -> BenchmarkOutcome:
    """Binary-search the largest request completed as one physical executor batch.

    Executors normally split at their configured size and adaptively halve provider
    limit failures. Both behaviors are useful in production but would make every
    benchmark candidate appear successful. The temporary ceiling and exact one-batch
    assertion ensure the measured value is the unsplit provider/executor limit.
    """

    if max_size < 1:
        raise ValueError("max benchmark size must be positive")
    original_batch_size = executor.batch_size
    executor.batch_size = max_size
    low, high = 1, max_size
    best = 0
    timings: list[float] = []
    winning_evidence: VerificationEvidence | None = None
    try:
        while low <= high:
            middle = (low + high) // 2
            calls = [
                ContractCall(
                    key=f"bench/{index}",
                    target=token_address,
                    calldata=TOTAL_SUPPLY_SELECTOR,
                )
                for index in range(middle)
            ]
            started = time.perf_counter()
            try:
                batches = await executor.execute(calls, anchor)
                if len(batches) != 1:
                    raise ServiceError("benchmark candidate was split into multiple batches")
                batch = batches[0]
                if not batch.evidence.verified:
                    raise ServiceError("benchmark batch is not verified")
                if tuple(result.key for result in batch.results) != tuple(
                    call.key for call in calls
                ):
                    raise ServiceError("benchmark executor returned partial or reordered results")
                if not all(result.success for result in batch.results):
                    raise ServiceError("benchmark contract call failed")
            except Exception:
                high = middle - 1
                continue
            timings.append(time.perf_counter() - started)
            best = middle
            winning_evidence = batch.evidence
            low = middle + 1
    finally:
        executor.batch_size = original_batch_size

    if best == 0 or winning_evidence is None:
        raise ServiceError("no safe single-batch size passed")
    return BenchmarkOutcome(
        max_batch_size=best,
        recommended_batch_size=max(1, int(best * 0.8)),
        timings_seconds=tuple(timings),
        evidence=winning_evidence,
    )


def _emit(event: str, **fields: object) -> None:
    """Emit structured progress without endpoint URLs or credentials."""

    print(json.dumps({"event": event, **fields}, sort_keys=True, default=str), flush=True)


class WriterGuard:
    """Coarse single-writer guard backed by append-only heartbeats."""

    def __init__(
        self,
        repository: ClickHouseRepository,
        *,
        chain_id: int,
        operation: str,
        stale_seconds: int,
    ) -> None:
        self.repository = repository
        self.chain_id = chain_id
        self.operation = operation
        self.stale_seconds = stale_seconds
        self.process_id = uuid4()
        self.started_at = datetime.now(UTC)
        self._task: asyncio.Task[None] | None = None
        self._acquired = False

    async def acquire(self) -> None:
        active = self.repository.fresh_writer_processes(
            self.chain_id, self.stale_seconds
        )
        if active:
            raise ServiceError(
                f"another writer heartbeat is fresh ({len(active)} process(es))"
            )
        self._write(datetime.now(UTC), "active")
        self._acquired = True
        after = self.repository.fresh_writer_processes(
            self.chain_id, self.stale_seconds
        )
        own = str(self.process_id)
        if any(process != own for process in after):
            try:
                self._write(datetime.fromtimestamp(0, UTC), "released")
            finally:
                self._acquired = False
            raise ServiceError("writer race detected; refusing overlapping execution")
        self._task = asyncio.create_task(self._heartbeat_loop())

    async def release(self) -> None:
        failures: list[BaseException] = []
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                failures.append(exc)
        if self._acquired:
            try:
                self._write(datetime.fromtimestamp(0, UTC), "released")
            except BaseException as exc:
                failures.append(exc)
            finally:
                self._acquired = False
        if failures:
            kinds = ", ".join(type(exc).__name__ for exc in failures)
            raise ServiceError(f"writer guard cleanup failed ({kinds})")

    @property
    def healthy(self) -> bool:
        return self._acquired and self._task is not None and not self._task.done()

    def ensure_healthy(self) -> None:
        if self.healthy:
            return
        task = self._task
        if task is not None and task.done() and not task.cancelled():
            # Retrieve the exception so asyncio does not emit an unhandled-task warning.
            task.exception()
        raise ServiceError("writer heartbeat stopped; refusing further work")

    def _write(self, heartbeat_at: datetime, state: str) -> None:
        self.repository.heartbeat(
            {
                "chain_id": self.chain_id,
                "process_id": self.process_id,
                "operation": self.operation,
                "hostname": socket.gethostname(),
                "details_json": json.dumps({"state": state}, sort_keys=True),
                "started_at": self.started_at,
                "heartbeat_at": heartbeat_at,
            }
        )

    async def _heartbeat_loop(self) -> None:
        interval = max(10.0, self.stale_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            self._write(datetime.now(UTC), "active")


class IndexerService:
    def __init__(self, settings: RuntimeSettings, operation: str) -> None:
        self.settings = settings
        self.operation = operation
        self.catalog: Catalog | None = None
        self.repository: ClickHouseRepository | None = None
        self.runtime: RpcRuntime | None = None
        self.guard: WriterGuard | None = None

    async def open(self) -> IndexerService:
        catalog = build_catalog(self.settings)
        validate_runtime_catalog(catalog, self.settings.abi_root)
        self.catalog = catalog
        try:
            repository = build_repository(self.settings)
            self.repository = repository
            repository.ping()
            self.runtime = build_rpc_runtime(self.settings, catalog)
            self.guard = WriterGuard(
                repository,
                chain_id=catalog.chain.chain_id,
                operation=self.operation,
                stale_seconds=self.settings.writer_stale_seconds,
            )
            await self.guard.acquire()
            await self._probe_endpoints()
            CatalogRegistrar(repository, catalog).register()
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self) -> None:
        failures: list[tuple[str, str]] = []
        guard = self.guard
        self.guard = None
        if guard is not None:
            try:
                await guard.release()
            except BaseException as exc:
                failures.append(("writer_guard", type(exc).__name__))
        runtime = self.runtime
        self.runtime = None
        if runtime is not None:
            try:
                await runtime.close()
            except BaseException as exc:
                failures.append(("rpc_runtime", type(exc).__name__))
        repository = self.repository
        self.repository = None
        if repository is not None:
            try:
                close = getattr(repository.client, "close", None)
                if callable(close):
                    close()
            except BaseException as exc:
                failures.append(("clickhouse", type(exc).__name__))
        self.catalog = None
        if failures:
            _emit(
                "service_cleanup_failed",
                failures=[
                    {"component": component, "error": error}
                    for component, error in failures
                ],
            )

    def _required(self) -> tuple[Catalog, ClickHouseRepository, RpcRuntime]:
        if self.catalog is None or self.repository is None or self.runtime is None:
            raise RuntimeError("service is not open")
        return self.catalog, self.repository, self.runtime

    async def _probe_endpoints(self) -> None:
        catalog, repository, runtime = self._required()
        multicall = catalog.chain.multicall3
        expected_hash = multicall.runtime_code_hash
        if expected_hash is None:
            raise ServiceError("Multicall3 runtime hash is not pinned")
        archive_token = earliest_archive_probe_token(catalog)
        successful = []
        for endpoint in runtime.rpc.endpoint_pool.endpoints:
            try:
                capability = await probe_endpoint_capabilities(
                    runtime.rpc,
                    endpoint,
                    expected_chain_id=catalog.chain.chain_id,
                    finality_tag=catalog.chain.finality_tag,
                    multicall_address=multicall.address,
                    multicall_deployment_block=multicall.deployment_block,
                    expected_multicall_code_hash=expected_hash,
                    archive_probe_address=archive_token.address,
                    archive_probe_block=archive_token.deployment_block,
                    archive_probe_calldata="0x" + TOTAL_SUPPLY_SELECTOR.hex(),
                )
            except Exception as exc:
                endpoint.healthy = False
                endpoint.cooldown_until = float("inf")
                ENDPOINT_HEALTHY.labels(
                    endpoint.provider_group, endpoint.fingerprint[:12]
                ).set(0)
                _emit(
                    "endpoint_probe",
                    provider_group=endpoint.provider_group,
                    endpoint_fingerprint=endpoint.fingerprint,
                    healthy=False,
                    error_class=type(exc).__name__,
                )
                continue
            successful.append(endpoint)
            ENDPOINT_HEALTHY.labels(
                endpoint.provider_group, endpoint.fingerprint[:12]
            ).set(1)
            _emit(
                "endpoint_probe",
                provider_group=endpoint.provider_group,
                endpoint_fingerprint=endpoint.fingerprint,
                healthy=True,
                supports_eip1898=bool(capability.supports_eip1898),
                supports_http_batch=bool(capability.supports_http_batch),
                supports_finality_tag=bool(capability.supports_finality_tag),
                archive_from_block=capability.archive_from_block,
                multicall_code_hash_verified=bool(capability.multicall_code_hash_verified),
            )
        if not successful:
            raise ServiceError("no RPC endpoint passed the safety probes")
        if not any(endpoint.supports_eip1898 for endpoint in successful):
            groups = {endpoint.provider_group for endpoint in successful}
            required = (
                catalog.chain.legacy_execution.number_fallback.required_provider_quorum
            )
            if len(groups) < required:
                raise ServiceError(
                    "no EIP-1898 endpoint and insufficient independent provider groups "
                    f"for number/hash quorum ({len(groups)}/{required})"
                )

    async def resolve_anchor(self, snapshot_date: date) -> BlockRef:
        catalog, repository, runtime = self._required()
        resolver = AnchorResolver(
            runtime.rpc,
            finality_tag=catalog.chain.finality_tag,
            fallback_confirmation_depth=catalog.chain.fallback_confirmation_depth,
        )
        resolved = await resolver.resolve(snapshot_date)
        anchor = AnchorStoreService(
            repository, catalog.chain.chain_id
        ).persist(resolved)
        _emit(
            "anchor_resolved",
            snapshot_date=snapshot_date,
            block_number=anchor.number,
            block_hash=anchor.block_hash,
        )
        return anchor

    def _jobs(self, job_name: str | None) -> tuple[JobConfig, ...]:
        catalog, _, _ = self._required()
        if job_name is not None:
            try:
                return (catalog.jobs[job_name],)
            except KeyError as exc:
                raise ServiceError(f"unknown job: {job_name}") from exc
        return tuple(catalog.jobs[name] for name in sorted(catalog.jobs))

    def _uses_full_holders(self, universe_name: str) -> bool:
        catalog, _, _ = self._required()

        def visit(name: str, seen: set[str]) -> bool:
            if name in seen:
                raise ServiceError(f"universe cycle while resolving {name}")
            selector: UniverseConfig = catalog.universes[name]
            if selector.kind == "full_holders":
                return True
            return any(visit(child, seen | {name}) for child in selector.of)

        return visit(universe_name, set())

    @staticmethod
    def _active(
        target: TokenConfig | PoolConfig,
        snapshot_date: date,
        anchor_block: int | None = None,
    ) -> bool:
        if target.date_start is not None and snapshot_date < target.date_start:
            return False
        if target.date_end is not None and snapshot_date >= target.date_end:
            return False
        # A target that is not yet deployed at the anchor is skipped, not attempted: reading its
        # state before it has code is a hard failure, which would otherwise abort the whole date.
        if anchor_block is not None and anchor_block < target.deployment_block:
            return False
        return True

    def _discovery_service(self) -> DiscoveryService:
        catalog, repository, runtime = self._required()
        discovery = catalog.chain.discovery
        return DiscoveryService(
            chain_id=catalog.chain.chain_id,
            rpc=runtime.rpc,
            store=repository,
            abi_registry=AbiRegistry(self.settings.abi_root),
            initial_chunk_size=discovery.initial_chunk_size,
            provider_result_cap=discovery.provider_result_cap,
        )

    async def discover(
        self,
        snapshot_date: date,
        *,
        job_name: str | None = None,
        anchor: BlockRef | None = None,
    ) -> BlockRef:
        catalog, _, _ = self._required()
        anchor = anchor or await self.resolve_anchor(snapshot_date)
        selected: dict[str, TokenConfig] = {}
        for job in self._jobs(job_name):
            if job.target_kind != "tokens":
                continue
            if job.universe is None or not self._uses_full_holders(job.universe):
                continue
            for token in catalog.token_targets(job):
                if self._active(token, snapshot_date, anchor.number):
                    selected[token.address] = token
        discovery = self._discovery_service()
        failures: list[str] = []
        for token in sorted(selected.values(), key=lambda item: item.address):
            try:
                await discovery.advance(
                    token,
                    anchor_block=anchor.number,
                    anchor_hash=anchor.block_hash,
                )
            except Exception as exc:
                # Surface the reason: a failed date otherwise only logs failure_count, forcing a
                # dig through discovery_ranges to find out what actually broke.
                _emit(
                    "discovery_failed",
                    token=token.symbol,
                    error=type(exc).__name__,
                    detail=str(exc)[:500],
                )
                failures.append(f"{token.symbol}: {type(exc).__name__}")
                continue
            _emit(
                "discovery_complete",
                token=token.symbol,
                through_block=anchor.number,
            )
        if failures:
            raise JobRunError(failures)
        return anchor

    def _runner(self) -> CensusRunner:
        catalog, repository, runtime = self._required()
        holder_repository = ClickHouseHolderUniverseRepository(
            repository,
            qualified_table=f"{repository.database}.holder_universe",
        )
        universes = UniverseResolver(
            chain_id=catalog.chain.chain_id,
            universes=catalog.universes,
            holder_repository=holder_repository,
            explicit_list_loader=catalog.explicit_addresses,
            seed_holders={
                token.address: token.seed_holders
                for token in catalog.tokens.values()
                if token.seed_holders
            },
        )
        return CensusRunner(
            catalog=catalog,
            store=repository,
            universe_resolver=universes,
            erc20_collector=Erc20Collector(runtime.executor),
            atoken_collector=ATokenCollector(runtime.executor),
            pool_collector=PoolReserveCollector(runtime.executor),
            balancer_collector=BalancerPoolCollector(
                runtime.executor,
                v2_vault=catalog.chain.balancer.v2_vault,
                v3_vault=catalog.chain.balancer.v3_vault,
            ),
            cl_collector=ClLiquidityCollector(
                runtime.executor,
                min_active_liquidity=self.settings.cl_min_active_liquidity,
            ),
            code_verifier=runtime.code_verifier,
        )

    async def census(
        self,
        snapshot_date: date,
        *,
        job_name: str | None = None,
    ) -> list[UUID]:
        catalog, repository, _ = self._required()
        anchor = await self.resolve_anchor(snapshot_date)
        # All full-holder discovery completes before any full-supply publication.
        await self.discover(snapshot_date, job_name=job_name, anchor=anchor)
        runner = self._runner()
        attempts: list[UUID] = []
        failures: list[str] = []
        for job in self._jobs(job_name):
            if job.target_kind == "tokens":
                for token in catalog.token_targets(job):
                    if not self._active(token, snapshot_date, anchor.number):
                        continue
                    if repository.publication_exists(
                        chain_id=catalog.chain.chain_id,
                        job_name=job.name,
                        target_kind="token",
                        target_address=token.address,
                        snapshot_date=snapshot_date,
                    ):
                        CENSUS_PUBLICATIONS.labels(job.name, "token", "skipped").inc()
                        _emit(
                            "census_skipped_published",
                            job=job.name,
                            target=token.symbol,
                            snapshot_date=snapshot_date,
                        )
                        continue
                    try:
                        attempt = await runner.run_token(
                            job, token, snapshot_date, anchor
                        )
                    except Exception as exc:
                        CENSUS_PUBLICATIONS.labels(job.name, "token", "failed").inc()
                        failures.append(
                            f"{job.name}/{token.symbol}: {type(exc).__name__}"
                        )
                        continue
                    attempts.append(attempt)
                    CENSUS_PUBLICATIONS.labels(job.name, "token", "published").inc()
                    _emit(
                        "census_published",
                        job=job.name,
                        target=token.symbol,
                        snapshot_date=snapshot_date,
                        attempt_id=attempt,
                    )
            else:
                for pool in catalog.pool_targets(job):
                    if not self._active(pool, snapshot_date, anchor.number):
                        continue
                    if repository.publication_exists(
                        chain_id=catalog.chain.chain_id,
                        job_name=job.name,
                        target_kind="pool",
                        target_address=pool.address,
                        snapshot_date=snapshot_date,
                    ):
                        CENSUS_PUBLICATIONS.labels(job.name, "pool", "skipped").inc()
                        continue
                    try:
                        attempt = await runner.run_pool(
                            job, pool, snapshot_date, anchor
                        )
                    except Exception as exc:
                        CENSUS_PUBLICATIONS.labels(job.name, "pool", "failed").inc()
                        failures.append(
                            f"{job.name}/{pool.name}: {type(exc).__name__}"
                        )
                        continue
                    attempts.append(attempt)
                    CENSUS_PUBLICATIONS.labels(job.name, "pool", "published").inc()
                    _emit(
                        "census_published",
                        job=job.name,
                        target=pool.name,
                        snapshot_date=snapshot_date,
                        attempt_id=attempt,
                    )
        if failures:
            raise JobRunError(failures)
        return attempts

    async def bench(self, snapshot_date: date, max_size: int = 600) -> int:
        catalog, repository, runtime = self._required()
        anchor = await self.resolve_anchor(snapshot_date)
        tokens = [
            token
            for token in catalog.tokens.values()
            if token.enabled and self._active(token, snapshot_date, anchor.number)
        ]
        if not tokens:
            raise ServiceError("no enabled token is active on the benchmark date")
        token = min(tokens, key=lambda item: item.address)
        await runtime.code_verifier.verify(token.address, anchor)
        selected_executor = runtime.executor.for_anchor(anchor)
        if not isinstance(selected_executor, BatchSizedExecutor):
            raise ServiceError("selected executor has no configurable batch size")
        outcome = await benchmark_single_batch_ceiling(
            selected_executor,
            token_address=token.address,
            anchor=anchor,
            max_size=max_size,
        )
        latency_ms = sorted(int(value * 1000) for value in outcome.timings_seconds)
        p50 = latency_ms[len(latency_ms) // 2]
        p95 = latency_ms[min(len(latency_ms) - 1, int(len(latency_ms) * 0.95))]
        _emit(
            "benchmark_complete",
            snapshot_date=snapshot_date,
            executor_kind=outcome.evidence.executor_kind.value,
            max_batch_size=outcome.max_batch_size,
            recommended_batch_size=outcome.recommended_batch_size,
            p50_latency_ms=p50,
            p95_latency_ms=p95,
        )
        return outcome.recommended_batch_size


async def _with_service(
    settings: RuntimeSettings,
    operation: str,
) -> IndexerService:
    return await IndexerService(settings, operation).open()


def _yesterday() -> date:
    return datetime.now(UTC).date() - timedelta(days=1)


def _date_range(from_date: date, to_date: date) -> Iterable[date]:
    current = from_date
    while current <= to_date:
        yield current
        current += timedelta(days=1)


def _month_end_dates(from_date: date, to_date: date) -> tuple[date, ...]:
    output: list[date] = []
    cursor = date(from_date.year, from_date.month, 1)
    while cursor <= to_date:
        last = date(
            cursor.year,
            cursor.month,
            calendar.monthrange(cursor.year, cursor.month)[1],
        )
        candidate = min(last, to_date)
        if candidate >= from_date:
            output.append(candidate)
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return tuple(dict.fromkeys(output))


async def run_discover(
    *,
    settings: RuntimeSettings,
    through: date | None,
    job: str | None,
) -> None:
    service = await _with_service(settings, "discover")
    try:
        await service.discover(through or _yesterday(), job_name=job)
    finally:
        await service.close()


async def run_census(
    *,
    settings: RuntimeSettings,
    snapshot_date: date,
    job: str | None,
) -> None:
    service = await _with_service(settings, "census")
    try:
        await service.census(snapshot_date, job_name=job)
    finally:
        await service.close()


async def run_backfill(
    *,
    settings: RuntimeSettings,
    from_date: date,
    to_date: date,
    job: str | None,
    daily: bool,
) -> None:
    service = await _with_service(settings, "backfill")
    # Serve /live, /ready, /metrics for the duration of the backfill so the same census metrics
    # the daemon exposes (calls, publications, batch latency, sentinel failures) are scrapable
    # during the long historical run. Readiness tracks the single-writer heartbeat.
    health: HealthServer | None = None
    try:
        health = start_health_server(
            settings.metrics_port,
            readiness_probe=lambda: service.guard is not None and service.guard.healthy,
        )
    except Exception as exc:
        _emit("health_server_start_failed", error=type(exc).__name__)
    try:
        dates = tuple(_date_range(from_date, to_date)) if daily else _month_end_dates(
            from_date, to_date
        )
        # Process every date; a failure on one date must not abort the whole range (a long
        # backfill will hit transient errors). Collect failed dates and surface them at the end.
        failed: list[str] = []
        for snapshot_date in dates:
            try:
                await service.census(snapshot_date, job_name=job)
            except JobRunError as exc:
                failed.append(f"{snapshot_date}({len(exc.failures)})")
                _emit(
                    "backfill_date_failed",
                    snapshot_date=snapshot_date,
                    failure_count=len(exc.failures),
                )
            except Exception as exc:
                # A non-JobRunError (e.g. a transient ClickHouse connection drop during a Cloud
                # node restart, or an RPC hiccup outside the per-target guard) must not crash the
                # whole multi-year ingest and drop into the pointless compute loop. Record the date
                # and continue; a re-run skips published dates via the gate and re-attempts these.
                failed.append(f"{snapshot_date}({type(exc).__name__})")
                _emit(
                    "backfill_date_failed",
                    snapshot_date=snapshot_date,
                    error=type(exc).__name__,
                    detail=str(exc)[:300],
                )
        if failed:
            raise ServiceError(
                f"backfill finished with {len(failed)}/{len(dates)} dates having target "
                f"failures: {', '.join(failed[:30])}"
                + (" ..." if len(failed) > 30 else "")
            )
    finally:
        await service.close()
        if health is not None:
            health.close()


async def run_densify(
    *,
    settings: RuntimeSettings,
    from_date: date,
    to_date: date,
    job: str | None,
) -> None:
    await run_backfill(
        settings=settings,
        from_date=from_date,
        to_date=to_date,
        job=job,
        daily=True,
    )


async def run_bench(
    *, settings: RuntimeSettings, snapshot_date: date | None
) -> None:
    service = await _with_service(settings, "bench")
    try:
        await service.bench(snapshot_date or _yesterday())
    finally:
        await service.close()


def run_compute(
    *,
    settings: RuntimeSettings,
    snapshot_date: date,
    module: str | None = None,
) -> None:
    """Recompute Layer 2 derived tables for one date. RPC-free: no runtime, no writer guard.

    Reads only published primitives and writes derived tables, so it needs the ClickHouse
    repository alone. Idempotent — re-running reproduces the same data rows.
    """
    catalog = build_catalog(settings)
    repository = build_repository(settings)
    repository.ping()
    try:
        modules = [m for m in COMPUTE_REGISTRY if module is None or m.name == module]
        if module is not None and not modules:
            known = ", ".join(sorted(m.name for m in COMPUTE_REGISTRY))
            raise ServiceError(f"unknown compute module {module!r}; known: {known}")
        for compute_module in modules:
            count = compute_module.compute(
                repository,
                chain_id=catalog.chain.chain_id,
                snapshot_date=snapshot_date,
            )
            COMPUTE_ROWS.labels(compute_module.name).inc(count)
            _emit(
                "compute_complete",
                module=compute_module.name,
                snapshot_date=snapshot_date,
                rows=count,
            )
    finally:
        close = getattr(repository.client, "close", None)
        if callable(close):
            close()


async def run_daemon(*, settings: RuntimeSettings) -> None:
    ready = False
    health: HealthServer | None = None
    service: IndexerService | None = None

    def readiness() -> bool:
        return (
            ready
            and service is not None
            and service.guard is not None
            and service.guard.healthy
        )

    try:
        health = start_health_server(
            settings.metrics_port, readiness_probe=readiness
        )
    except Exception as exc:
        _emit("health_server_start_failed", error=type(exc).__name__)
        health = None
    allowed_jobs = settings.daemon_job_names()
    try:
        service = await _with_service(settings, "daemon")
        ready = True
        while True:
            if service.guard is None:
                raise ServiceError("daemon writer guard is unavailable")
            service.guard.ensure_healthy()
            DAEMON_CYCLES.inc()
            target = _yesterday()
            for job in service._jobs(None):
                if job.cadence != "daily":
                    continue
                if allowed_jobs is not None and job.name not in allowed_jobs:
                    continue
                try:
                    await service.census(target, job_name=job.name)
                except JobRunError as exc:
                    DAEMON_JOB_FAILURES.labels(job.name).inc()
                    _emit(
                        "daemon_job_failed",
                        job=job.name,
                        failure_count=len(exc.failures),
                    )
                service.guard.ensure_healthy()
            await asyncio.sleep(settings.daemon_poll_seconds)
    finally:
        ready = False
        if service is not None:
            await service.close()
        if health is not None:
            try:
                health.close()
            except Exception as exc:
                _emit("health_server_stop_failed", error=type(exc).__name__)
