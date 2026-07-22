from __future__ import annotations

import re
from datetime import date
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, model_validator

from rpc_state_indexer.domain import IntegrityMode

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def normalize_address(value: str) -> str:
    if not ADDRESS_RE.fullmatch(value):
        raise ValueError(f"invalid EVM address: {value!r}")
    return value.lower()


def normalize_hash(value: str) -> str:
    if not HASH_RE.fullmatch(value):
        raise ValueError(f"invalid 32-byte hash: {value!r}")
    return value.lower()


Address = Annotated[str, AfterValidator(normalize_address)]
Hash32 = Annotated[str, AfterValidator(normalize_hash)]


class MulticallConfig(BaseModel):
    address: Address
    deployment_block: int = Field(ge=0)
    runtime_code_hash: Hash32 | None = None
    default_batch_size: int = Field(default=250, ge=1)


class NumberFallbackConfig(BaseModel):
    enabled: bool = True
    required_provider_quorum: int = Field(default=2, ge=2)
    require_distinct_provider_groups: bool = True
    hash_sandwich: bool = True


class LegacyExecutionConfig(BaseModel):
    enabled: bool = True
    default_batch_size: int = Field(default=100, ge=1)
    preferred_block_reference: Literal["eip1898"] = "eip1898"
    number_fallback: NumberFallbackConfig = Field(default_factory=NumberFallbackConfig)


class DiscoveryConfig(BaseModel):
    initial_chunk_size: int = Field(default=10_000, ge=1)
    provider_result_cap: int = Field(default=10_000, ge=1)


class BalancerConfig(BaseModel):
    """Chain-singleton Balancer Vault targets. Absent by default."""

    v2_vault: Address | None = None
    v3_vault: Address | None = None


class ChainConfig(BaseModel):
    name: str
    chain_id: int = Field(ge=1)
    finality_tag: str = "finalized"
    fallback_confirmation_depth: int = Field(default=64, ge=1)
    expected_block_time_seconds: float = Field(default=5.0, gt=0)
    multicall3: MulticallConfig
    legacy_execution: LegacyExecutionConfig = Field(default_factory=LegacyExecutionConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    balancer: BalancerConfig = Field(default_factory=BalancerConfig)


class EventConfig(BaseModel):
    abi: str
    event: str
    holder_topics: list[int]

    @model_validator(mode="after")
    def valid_topics(self) -> EventConfig:
        if not self.holder_topics:
            raise ValueError("holder_topics cannot be empty")
        if any(topic not in {1, 2, 3} for topic in self.holder_topics):
            raise ValueError("holder topic positions must be in 1..3")
        if len(set(self.holder_topics)) != len(self.holder_topics):
            raise ValueError("holder topic positions must be unique")
        return self


class IndexSourceConfig(BaseModel):
    contract: Address
    function: Literal["getReserveNormalizedIncome"]
    argument: Address
    output_name: Literal["liquidity_index_ray"] = "liquidity_index_ray"


class TokenConfig(BaseModel):
    address: Address
    symbol: str
    decimals: int = Field(ge=0, le=255)
    token_class: Literal[
        "standard_erc20",
        "weth9_fork",
        "aave_v3_atoken",
        "spark_atoken",
    ]
    deployment_block: int = Field(ge=0)
    date_start: date | None = None
    date_end: date | None = None
    enabled: bool = True
    zero_address_role: Literal["event_sentinel", "holder"] = "event_sentinel"
    balance_function: Literal["balanceOf", "scaledBalanceOf"] = "balanceOf"
    supply_functions: list[str] = Field(default_factory=lambda: ["totalSupply"])
    discovery_events: list[EventConfig]
    seed_holders: list[Address] = Field(default_factory=list)
    index_source: IndexSourceConfig | None = None

    @property
    def is_atoken(self) -> bool:
        return self.token_class in {"aave_v3_atoken", "spark_atoken"}

    @model_validator(mode="after")
    def valid_token_semantics(self) -> TokenConfig:
        if self.date_end is not None and self.date_start is not None:
            if self.date_end <= self.date_start:
                raise ValueError("date_end must be after date_start")
        if self.is_atoken:
            if self.balance_function != "scaledBalanceOf":
                raise ValueError("aToken balance_function must be scaledBalanceOf")
            if "scaledTotalSupply" not in self.supply_functions:
                raise ValueError("aToken must read scaledTotalSupply")
            if self.index_source is None:
                raise ValueError("aToken requires index_source")
        elif self.index_source is not None:
            raise ValueError("only aTokens may declare index_source")
        zero = "0x0000000000000000000000000000000000000000"
        if self.zero_address_role == "event_sentinel" and zero in self.seed_holders:
            raise ValueError("zero address cannot be seeded when it is an event sentinel")
        return self


class PoolAssetConfig(BaseModel):
    token: Address


BALANCER_V2_CLASS = "balancer_v2"
BALANCER_V3_CLASS = "balancer_v3"
BALANCER_POOL_CLASSES = frozenset({BALANCER_V2_CLASS, BALANCER_V3_CLASS})

UNISWAP_V3_CLASS = "uniswap_v3"
SWAPR_V3_ALGEBRA_CLASS = "swapr_v3_algebra"
CL_POOL_CLASSES = frozenset({UNISWAP_V3_CLASS, SWAPR_V3_ALGEBRA_CLASS})


class PoolConfig(BaseModel):
    address: Address
    name: str
    pool_class: str
    deployment_block: int = Field(ge=0)
    date_start: date | None = None
    date_end: date | None = None
    enabled: bool = True
    assets: list[PoolAssetConfig] = Field(min_length=1)
    # Balancer V2 pools are keyed by a 32-byte poolId; V3 pools (and AMM pools whose
    # reserves are the pool's own balanceOf) are keyed by their contract address.
    pool_id: Hash32 | None = None
    # Concentrated-liquidity immutables, optionally captured at enumeration to avoid a
    # per-anchor read. When absent the CL collector reads them on-chain.
    tick_spacing: int | None = Field(default=None, ge=1)
    fee: int | None = Field(default=None, ge=0)

    @property
    def is_balancer(self) -> bool:
        return self.pool_class in BALANCER_POOL_CLASSES

    @property
    def is_cl(self) -> bool:
        return self.pool_class in CL_POOL_CLASSES

    @model_validator(mode="after")
    def valid_pool_semantics(self) -> PoolConfig:
        if (
            self.date_end is not None
            and self.date_start is not None
            and self.date_end <= self.date_start
        ):
            raise ValueError("date_end must be after date_start")
        if self.pool_class == BALANCER_V2_CLASS and self.pool_id is None:
            raise ValueError("balancer_v2 pool requires pool_id")
        if self.pool_class != BALANCER_V2_CLASS and self.pool_id is not None:
            raise ValueError("pool_id is only valid for balancer_v2 pools")
        if not self.is_cl and (self.tick_spacing is not None or self.fee is not None):
            raise ValueError("tick_spacing/fee are only valid for concentrated-liquidity pools")
        return self


class UniverseConfig(BaseModel):
    kind: Literal["full_holders", "explicit_list", "union", "intersect"]
    source: str | None = None
    address_column: str = "address"
    of: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_shape(self) -> UniverseConfig:
        if self.kind == "explicit_list" and not self.source:
            raise ValueError("explicit_list requires source")
        if self.kind == "explicit_list" and self.source is not None:
            source = PurePosixPath(self.source)
            if (
                source.is_absolute()
                or ".." in source.parts
                or not source.parts
                or source.parts[0] != "vendored"
                or source.suffix.lower() != ".csv"
            ):
                raise ValueError(
                    "explicit_list source must be a vendored/*.csv relative path"
                )
        if self.kind in {"union", "intersect"} and len(self.of) < 2:
            raise ValueError(f"{self.kind} requires at least two members")
        return self


class TokenSelector(BaseModel):
    addresses: list[Address] = Field(default_factory=list)
    class_in: list[str] = Field(default_factory=list)
    all_enabled: bool = False

    @model_validator(mode="after")
    def exactly_one_selector(self) -> TokenSelector:
        choices = int(bool(self.addresses)) + int(bool(self.class_in)) + int(self.all_enabled)
        if choices != 1:
            raise ValueError("choose exactly one token selector")
        return self


class PoolSelector(BaseModel):
    addresses: list[Address] = Field(default_factory=list)
    class_in: list[str] = Field(default_factory=list)
    all_enabled: bool = False

    @model_validator(mode="after")
    def exactly_one_selector(self) -> PoolSelector:
        choices = int(bool(self.addresses)) + int(bool(self.class_in)) + int(self.all_enabled)
        if choices != 1:
            raise ValueError("choose exactly one pool selector")
        return self


class JobConfig(BaseModel):
    name: str
    target_kind: Literal["tokens", "pools"]
    token_selector: TokenSelector | None = None
    pool_selector: PoolSelector | None = None
    universe: str | None = None
    cadence: Literal["daily", "manual"] = "daily"
    integrity_mode: IntegrityMode
    coverage_start: date | None = None

    @model_validator(mode="after")
    def valid_job_shape(self) -> JobConfig:
        if self.target_kind == "tokens":
            if self.token_selector is None or self.pool_selector is not None:
                raise ValueError("token job requires token_selector only")
            if self.universe is None:
                raise ValueError("token job requires universe")
            if self.integrity_mode is IntegrityMode.POOL_ASSETS:
                raise ValueError("token job cannot use pool_assets integrity")
        else:
            if self.pool_selector is None or self.token_selector is not None:
                raise ValueError("pool job requires pool_selector only")
            if self.universe is not None:
                raise ValueError("pool job does not use an address universe")
            if self.integrity_mode not in {
                IntegrityMode.POOL_ASSETS,
                IntegrityMode.CL_LIQUIDITY,
            }:
                raise ValueError("pool job requires pool_assets or cl_liquidity integrity")
        return self
