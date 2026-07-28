from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True, slots=True)
class EndpointSetting:
    name: str
    url: str
    provider_group: str


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
    )

    chain: str = Field(default="gnosis", alias="CHAIN")
    config_root: Path = Field(default=Path("config"), alias="CONFIG_ROOT")
    abi_root: Path = Field(default=Path("abis"), alias="ABI_ROOT")
    migrations_dir: Path = Field(default=Path("migrations"), alias="MIGRATIONS_DIR")
    rpc_urls: str = Field(default="", alias="RPC_URLS")
    rpc_provider_groups: str = Field(default="", alias="RPC_PROVIDER_GROUPS")

    clickhouse_host: str = Field(default="", alias="CLICKHOUSE_HOST")
    clickhouse_port: int = Field(default=8443, alias="CLICKHOUSE_PORT")
    clickhouse_user: str = Field(default="default", alias="CLICKHOUSE_USER")
    clickhouse_password: SecretStr = Field(default=SecretStr(""), alias="CLICKHOUSE_PASSWORD")
    clickhouse_database: str = Field(default="rpc_indexer", alias="CLICKHOUSE_DATABASE")
    clickhouse_secure: bool = Field(default=True, alias="CLICKHOUSE_SECURE")
    clickhouse_verify: bool = Field(default=True, alias="CLICKHOUSE_VERIFY")

    rpc_concurrency: int = Field(default=8, alias="RPC_CONCURRENCY", ge=1)
    rpc_requests_per_second: int = Field(default=30, alias="RPC_REQUESTS_PER_SECOND", ge=1)
    multicall_batch_size: int = Field(default=250, alias="MULTICALL_BATCH_SIZE", ge=1)
    legacy_rpc_batch_size: int = Field(default=100, alias="LEGACY_RPC_BATCH_SIZE", ge=1)
    max_retries: int = Field(default=5, alias="MAX_RETRIES", ge=1)
    writer_stale_seconds: int = Field(default=120, alias="WRITER_STALE_SECONDS", ge=30)
    # Pools whose active liquidity() is below this are ingested state-only (no tick sweep).
    cl_min_active_liquidity: int = Field(
        default=0, alias="CL_MIN_ACTIVE_LIQUIDITY", ge=0
    )
    # A discovered target whose last N census attempts all failed is excluded from new
    # batches (recorded, never silently zeroed). Curated targets are never quarantined.
    discovered_quarantine_threshold: int = Field(
        default=3, alias="DISCOVERED_QUARANTINE_THRESHOLD", ge=1
    )
    metrics_port: int = Field(default=9090, alias="METRICS_PORT", ge=1, le=65535)
    daemon_poll_seconds: int = Field(default=300, alias="DAEMON_POLL_SECONDS", ge=10)
    # Comma-separated job names the daemon runs each cycle; empty = every daily job. Use this to
    # scope a single daemon away from the full multi-thousand-target catalog.
    daemon_jobs: str = Field(default="", alias="DAEMON_JOBS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("clickhouse_database")
    @classmethod
    def valid_database(cls, value: str) -> str:
        if not value.replace("_", "a").isalnum() or value[0].isdigit():
            raise ValueError("invalid ClickHouse database name")
        return value

    def daemon_job_names(self) -> frozenset[str] | None:
        """The job names the daemon should run, or None to run every daily job."""
        names = frozenset(item.strip() for item in self.daemon_jobs.split(",") if item.strip())
        return names or None

    def endpoints(self, *, required: bool = True) -> tuple[EndpointSetting, ...]:
        urls = [item.strip() for item in self.rpc_urls.split(",") if item.strip()]
        groups = [item.strip() for item in self.rpc_provider_groups.split(",") if item.strip()]

        if not urls and not required:
            return ()
        if not urls:
            raise ValueError("RPC_URLS is required")
        if groups and len(groups) != len(urls):
            raise ValueError("RPC_PROVIDER_GROUPS must match RPC_URLS length")
        if not groups:
            # Unknown provenance must never be promoted into an independence proof.
            # EIP-1898-capable endpoints can still be used normally, but the numeric
            # block fallback cannot form a false quorum from unlabeled URLs.
            groups = ["unclassified"] * len(urls)

        return tuple(
            EndpointSetting(f"rpc_{index + 1}", url, groups[index])
            for index, url in enumerate(urls)
        )
