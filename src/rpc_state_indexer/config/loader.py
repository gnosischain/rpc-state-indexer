from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from rpc_state_indexer.config.hashing import canonical_hash, file_hash
from rpc_state_indexer.config.models import (
    BALANCER_V2_CLASS,
    BALANCER_V3_CLASS,
    ChainConfig,
    JobConfig,
    PoolConfig,
    SweepConfig,
    TokenConfig,
    UniverseConfig,
    normalize_address,
)
from rpc_state_indexer.domain import IntegrityMode
from rpc_state_indexer.errors import ConfigError


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate keys at every nesting level."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _read_yaml_optional(path: Path) -> dict[str, Any]:
    """Read a catalog file that older chains may not have yet."""

    if not path.is_file():
        return {}
    return _read_yaml(path)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        value = yaml.load(path.read_text(), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"configuration root must be a mapping: {path}")
    return value


@dataclass(frozen=True, slots=True)
class Catalog:
    root: Path
    chain: ChainConfig
    tokens: dict[str, TokenConfig]
    pools: dict[str, PoolConfig]
    universes: dict[str, UniverseConfig]
    jobs: dict[str, JobConfig]
    sweeps: dict[str, SweepConfig] = field(default_factory=dict)

    def token_targets(self, job: JobConfig) -> tuple[TokenConfig, ...]:
        selector = job.token_selector
        if selector is None:
            return ()
        if selector.discovered:
            # Resolved at runtime from the sweep candidates; the static catalog
            # deliberately holds no list for these jobs.
            return ()
        if selector.all_enabled:
            values = [token for token in self.tokens.values() if token.enabled]
        elif selector.addresses:
            values = [self.tokens[address] for address in selector.addresses]
        else:
            selected = set(selector.class_in)
            values = [
                token for token in self.tokens.values()
                if token.enabled and token.token_class in selected
            ]
        return tuple(sorted(values, key=lambda token: token.address))

    def pool_targets(self, job: JobConfig) -> tuple[PoolConfig, ...]:
        selector = job.pool_selector
        if selector is None:
            return ()
        if selector.all_enabled:
            values = [pool for pool in self.pools.values() if pool.enabled]
        elif selector.addresses:
            values = [self.pools[address] for address in selector.addresses]
        else:
            selected = set(selector.class_in)
            values = [
                pool for pool in self.pools.values()
                if pool.enabled and pool.pool_class in selected
            ]
        return tuple(sorted(values, key=lambda pool: pool.address))

    def explicit_addresses(self, universe_name: str) -> tuple[str, ...]:
        universe = self.universes[universe_name]
        if universe.kind != "explicit_list" or universe.source is None:
            raise ConfigError(f"{universe_name} is not an explicit_list")
        path = self.root / self.chain.name / universe.source
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            if universe.address_column not in (reader.fieldnames or []):
                raise ConfigError(f"missing {universe.address_column!r} in {path}")
            return tuple(
                sorted({normalize_address(row[universe.address_column]) for row in reader})
            )

    def target_effective_config(
        self, job: JobConfig, target: TokenConfig | PoolConfig
    ) -> dict[str, Any]:
        """Return the complete, canonical payload identified by ``config_hash``.

        Keeping this payload public prevents persistence code from recording a
        human-readable config document that does not actually hash to the registry's
        ``config_hash`` (notably for composed universes and vendored address lists).
        """

        universe_value: Any = None
        vendor_hashes: dict[str, str] = {}
        if job.universe is not None:
            universe_value = self._expanded_universe(job.universe, set())
            self._collect_vendor_hashes(job.universe, vendor_hashes, set())
        chain_value = self.chain.model_dump(mode="json")
        # These fields change throughput only, never the observed state or scope.
        chain_value.pop("expected_block_time_seconds", None)
        chain_value.pop("discovery", None)
        chain_value["multicall3"].pop("default_batch_size", None)
        chain_value["legacy_execution"].pop("default_batch_size", None)
        job_value = job.model_dump(mode="json")
        job_value.pop("cadence", None)
        # A static selector resolves the same targets by the same means as it did before
        # `discovered` existed, so hashing the false flag would invalidate every
        # already-published row for a no-op. A true flag is a real scope change and stays.
        selector = job_value.get("token_selector")
        if isinstance(selector, dict) and not selector.get("discovered"):
            selector.pop("discovered", None)
        return {
            "chain": chain_value,
            "job": job_value,
            "target": target.model_dump(mode="json"),
            "universe": universe_value,
            "vendor_hashes": vendor_hashes,
        }

    def target_config_hash(self, job: JobConfig, target: TokenConfig | PoolConfig) -> str:
        return canonical_hash(self.target_effective_config(job, target))

    def _expanded_universe(self, name: str, stack: set[str]) -> dict[str, Any]:
        if name in stack:
            raise ConfigError(f"universe reference cycle at {name}")
        try:
            universe = self.universes[name]
        except KeyError as exc:
            raise ConfigError(f"unknown universe selector: {name}") from exc
        if universe.kind not in {"union", "intersect"}:
            return universe.model_dump(mode="json")
        return {
            "kind": universe.kind,
            "of": [self._expanded_universe(child, stack | {name}) for child in universe.of],
        }

    def _collect_vendor_hashes(
        self, name: str, output: dict[str, str], stack: set[str]
    ) -> None:
        if name in stack:
            raise ConfigError(f"universe reference cycle at {name}")
        try:
            universe = self.universes[name]
        except KeyError as exc:
            raise ConfigError(f"unknown universe selector: {name}") from exc
        if universe.kind == "explicit_list" and universe.source is not None:
            path = self.root / self.chain.name / universe.source
            output[universe.source] = file_hash(path)
        for child in universe.of:
            self._collect_vendor_hashes(child, output, stack | {name})

    def summary(self) -> str:
        enabled_tokens = sum(token.enabled for token in self.tokens.values())
        enabled_pools = sum(pool.enabled for pool in self.pools.values())
        return (
            f"chain={self.chain.name} chain_id={self.chain.chain_id} "
            f"tokens={enabled_tokens}/{len(self.tokens)} "
            f"pools={enabled_pools}/{len(self.pools)} jobs={len(self.jobs)}"
        )


def load_catalog(config_root: Path, chain_name: str) -> Catalog:
    chains_raw = _read_yaml(config_root / "chains.yaml").get("chains", {})
    if chain_name not in chains_raw:
        raise ConfigError(f"unknown chain: {chain_name}")
    chain = ChainConfig(name=chain_name, **chains_raw[chain_name])
    chain_root = config_root / chain_name

    token_rows = _read_yaml(chain_root / "tokens.yaml").get("tokens", [])
    pool_rows = _read_yaml(chain_root / "pools.yaml").get("pools", [])
    universe_rows = _read_yaml(chain_root / "universes.yaml").get("universes", {})
    job_rows = _read_yaml(chain_root / "jobs.yaml").get("jobs", {})
    sweep_rows = _read_yaml_optional(chain_root / "sweeps.yaml").get("sweeps", {})

    token_values = tuple(map(TokenConfig.model_validate, token_rows))
    pool_values = tuple(map(PoolConfig.model_validate, pool_rows))
    tokens = {token.address: token for token in token_values}
    pools = {pool.address: pool for pool in pool_values}
    if len(tokens) != len(token_values):
        raise ConfigError("token catalog contains a duplicate address")
    if len(pools) != len(pool_values):
        raise ConfigError("pool catalog contains a duplicate address")
    universes = {
        name: UniverseConfig.model_validate(value) for name, value in universe_rows.items()
    }
    jobs = {
        name: JobConfig(name=name, **value) for name, value in job_rows.items()
    }
    sweeps = {
        name: SweepConfig(name=name, **value) for name, value in sweep_rows.items()
    }

    catalog = Catalog(config_root, chain, tokens, pools, universes, jobs, sweeps)
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog: Catalog) -> None:
    for pool in catalog.pools.values():
        missing = [asset.token for asset in pool.assets if asset.token not in catalog.tokens]
        if missing:
            raise ConfigError(f"pool {pool.address} references unknown tokens: {missing}")
        if pool.pool_class == BALANCER_V2_CLASS and catalog.chain.balancer.v2_vault is None:
            raise ConfigError(
                f"pool {pool.address} is balancer_v2 but chain defines no balancer.v2_vault"
            )
        if pool.pool_class == BALANCER_V3_CLASS and catalog.chain.balancer.v3_vault is None:
            raise ConfigError(
                f"pool {pool.address} is balancer_v3 but chain defines no balancer.v3_vault"
            )

    for job in catalog.jobs.values():
        if job.universe is not None and job.universe not in catalog.universes:
            raise ConfigError(f"job {job.name} references unknown universe {job.universe}")
        if job.target_kind == "tokens":
            selector = job.token_selector
            if selector is not None and selector.discovered:
                if job.integrity_mode is not IntegrityMode.SCOPED:
                    raise ConfigError(
                        f"job {job.name}: discovered targets require scoped integrity"
                    )
                universe = catalog.universes.get(job.universe or "")
                if universe is None or universe.kind != "explicit_list":
                    raise ConfigError(
                        f"job {job.name}: discovered targets require an "
                        "explicit_list universe"
                    )
                continue
            targets = catalog.token_targets(job)
            if not targets:
                raise ConfigError(f"job {job.name} selects no tokens")
            if job.integrity_mode in {
                IntegrityMode.FULL_SUPPLY,
                IntegrityMode.SCALED_FULL_SUPPLY,
            }:
                if job.universe != "full_holders":
                    raise ConfigError(f"job {job.name}: exact supply requires full_holders")
            for token in targets:
                if job.integrity_mode is IntegrityMode.SCALED_FULL_SUPPLY and not token.is_atoken:
                    raise ConfigError(f"job {job.name}: {token.symbol} is not an aToken")
                if job.integrity_mode is IntegrityMode.FULL_SUPPLY and token.is_atoken:
                    raise ConfigError(f"job {job.name}: aToken requires scaled_full_supply")
        elif not catalog.pool_targets(job):
            raise ConfigError(f"job {job.name} selects no pools")

    for sweep in catalog.sweeps.values():
        universe = catalog.universes.get(sweep.universe)
        if universe is None:
            raise ConfigError(
                f"sweep {sweep.name} references unknown universe {sweep.universe}"
            )
        # A sweep scans by wallet topics, so its universe must be a closed address list —
        # never a discovered set (full_holders would make discovery input depend on
        # discovery output).
        if universe.kind != "explicit_list":
            raise ConfigError(
                f"sweep {sweep.name}: universe {sweep.universe} must be an explicit_list"
            )
        if not catalog.explicit_addresses(sweep.universe):
            raise ConfigError(f"sweep {sweep.name}: universe {sweep.universe} is empty")

    for name in catalog.universes:
        catalog._expanded_universe(name, set())
