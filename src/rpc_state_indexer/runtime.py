"""Explicit runtime construction with no import-time network connections."""

from __future__ import annotations

from dataclasses import dataclass

from rpc_state_indexer.config.loader import Catalog, load_catalog
from rpc_state_indexer.config.models import TokenConfig
from rpc_state_indexer.execution.code import HistoricalCodeVerifier
from rpc_state_indexer.execution.legacy_rpc_batch import LegacyRpcBatchExecutor
from rpc_state_indexer.execution.multicall3 import Multicall3Executor
from rpc_state_indexer.execution.router import HistoricalExecutorRouter
from rpc_state_indexer.rpc.client import AsyncRpcClient
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.endpoint_pool import EndpointPool
from rpc_state_indexer.settings import RuntimeSettings
from rpc_state_indexer.storage.clickhouse import (
    ClickHouseConnectionSettings,
    create_clickhouse_client,
)
from rpc_state_indexer.storage.repositories import ClickHouseRepository


@dataclass(slots=True)
class RpcRuntime:
    catalog: Catalog
    rpc: AsyncRpcClient
    executor: HistoricalExecutorRouter
    code_verifier: HistoricalCodeVerifier

    async def close(self) -> None:
        await self.rpc.close()


def build_catalog(settings: RuntimeSettings) -> Catalog:
    return load_catalog(settings.config_root, settings.chain)


def earliest_archive_probe_token(catalog: Catalog) -> TokenConfig:
    """Return the oldest enabled token whose state bounds archive support."""

    enabled_tokens = tuple(token for token in catalog.tokens.values() if token.enabled)
    if not enabled_tokens:
        raise ValueError(
            "at least one enabled token is required for the archive capability probe"
        )
    return min(
        enabled_tokens,
        key=lambda token: (token.deployment_block, token.address),
    )


def build_rpc_runtime(settings: RuntimeSettings, catalog: Catalog) -> RpcRuntime:
    endpoints = tuple(
        RpcEndpoint(
            item.name,
            item.url,
            item.provider_group,
            requests_per_second=float(settings.rpc_requests_per_second),
        )
        for item in settings.endpoints(required=True)
    )
    pool = EndpointPool(endpoints)
    rpc = AsyncRpcClient(
        pool,
        concurrency=settings.rpc_concurrency,
        max_retries=settings.max_retries,
    )
    multicall_config = catalog.chain.multicall3
    legacy_config = catalog.chain.legacy_execution
    multicall = Multicall3Executor(
        rpc,
        address=multicall_config.address,
        deployment_block=multicall_config.deployment_block,
        batch_size=settings.multicall_batch_size,
        # Independent batches go out together; the client's own semaphore and rate limiter
        # remain the real ceiling, so this just stops them from sitting idle.
        max_parallel_batches=settings.rpc_concurrency,
    )
    legacy = LegacyRpcBatchExecutor(
        rpc,
        batch_size=settings.legacy_rpc_batch_size,
        required_provider_quorum=(
            legacy_config.number_fallback.required_provider_quorum
        ),
    )
    router = HistoricalExecutorRouter(
        multicall_deployment_block=multicall_config.deployment_block,
        multicall_executor=multicall,
        legacy_executor=legacy,
    )
    code_verifier = HistoricalCodeVerifier(
        rpc,
        number_provider_quorum=(
            legacy_config.number_fallback.required_provider_quorum
        ),
    )
    return RpcRuntime(catalog, rpc, router, code_verifier)


def clickhouse_connection_settings(
    settings: RuntimeSettings,
) -> ClickHouseConnectionSettings:
    return ClickHouseConnectionSettings(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password.get_secret_value(),
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
        verify=settings.clickhouse_verify,
    )


def build_repository(settings: RuntimeSettings) -> ClickHouseRepository:
    connection = clickhouse_connection_settings(settings)
    client = create_clickhouse_client(connection)
    return ClickHouseRepository(
        client,
        settings.clickhouse_database,
        client_factory=lambda: create_clickhouse_client(connection),
    )
