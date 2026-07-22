"""ClickHouse persistence primitives for rpc-state-indexer."""

from .clickhouse import ClickHouseConnectionSettings, create_clickhouse_client
from .digests import (
    BalanceDigestRow,
    PoolBalanceDigestRow,
    ScalarDigestRow,
    digest_pool_observations,
    digest_token_observations,
    digest_universe,
)
from .migrations import MigrationError, MigrationRunner
from .repositories import ClickHouseRepository

__all__ = [
    "BalanceDigestRow",
    "ClickHouseConnectionSettings",
    "ClickHouseRepository",
    "MigrationError",
    "MigrationRunner",
    "PoolBalanceDigestRow",
    "ScalarDigestRow",
    "create_clickhouse_client",
    "digest_pool_observations",
    "digest_token_observations",
    "digest_universe",
]
