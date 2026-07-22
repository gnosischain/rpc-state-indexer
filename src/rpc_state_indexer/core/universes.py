"""Resolve named address universes into immutable, auditable memberships."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from rpc_state_indexer.config.models import UniverseConfig, normalize_address
from rpc_state_indexer.domain import FrozenUniverse
from rpc_state_indexer.errors import ConfigError
from rpc_state_indexer.storage.digests import digest_universe

_QUALIFIED_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class UniverseMember:
    """One address discovered by a holder source, with stable provenance labels."""

    address: str
    sources: tuple[str, ...] = ("own_scan",)


class HolderUniverseRepository(Protocol):
    """Read-only boundary implemented by ClickHouse and in-memory test fakes."""

    def holder_members(
        self,
        *,
        chain_id: int,
        token_address: str,
        anchor_block: int,
    ) -> Iterable[UniverseMember]: ...


class QueryRowsRepository(Protocol):
    """Small subset of the ClickHouse repository used by the holder adapter."""

    def query_rows(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...


class ClickHouseHolderUniverseRepository:
    """Adapt the append-only ``holder_universe`` table to the selector protocol."""

    def __init__(
        self,
        repository: QueryRowsRepository,
        *,
        qualified_table: str = "rpc_indexer.holder_universe",
    ) -> None:
        if not _QUALIFIED_TABLE_RE.fullmatch(qualified_table):
            raise ValueError("holder universe table must be a qualified identifier")
        self.repository = repository
        self.qualified_table = qualified_table

    def holder_members(
        self,
        *,
        chain_id: int,
        token_address: str,
        anchor_block: int,
    ) -> tuple[UniverseMember, ...]:
        rows = self.repository.query_rows(
            f"""
            SELECT
                holder_address,
                arraySort(groupUniqArray(
                    if(source_detail = '', source, concat(source, ':', source_detail))
                )) AS member_sources
            FROM {self.qualified_table} FINAL
            WHERE chain_id = {{chain_id:UInt64}}
              AND token_address = {{token_address:String}}
              AND first_seen_block <= {{anchor_block:UInt64}}
            GROUP BY holder_address
            ORDER BY holder_address
            """,
            {
                "chain_id": chain_id,
                "token_address": normalize_address(token_address),
                "anchor_block": anchor_block,
            },
        )
        return tuple(
            UniverseMember(
                address=str(row["holder_address"]),
                sources=tuple(str(source) for source in row["member_sources"]),
            )
            for row in rows
        )


class UniverseResolver:
    """Resolve full, explicit, union, and intersection selectors recursively.

    The resolver freezes membership for a specific token and anchor. It never reads a
    live file itself: the supplied explicit-list loader is responsible for returning
    the vendored list selected by the catalog.
    """

    def __init__(
        self,
        *,
        chain_id: int,
        universes: Mapping[str, UniverseConfig],
        holder_repository: HolderUniverseRepository,
        explicit_list_loader: Callable[[str], Iterable[str]],
        seed_holders: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        if chain_id < 1:
            raise ValueError("chain_id must be positive")
        self.chain_id = chain_id
        self.universes = dict(universes)
        self.holder_repository = holder_repository
        self.explicit_list_loader = explicit_list_loader
        self.seed_holders = {
            normalize_address(token): tuple(normalize_address(item) for item in holders)
            for token, holders in (seed_holders or {}).items()
        }

    def resolve(
        self,
        universe_name: str,
        *,
        token_address: str,
        anchor_block: int,
    ) -> FrozenUniverse:
        if anchor_block < 0:
            raise ValueError("anchor_block cannot be negative")
        token_address = normalize_address(token_address)
        members = self._resolve_map(
            universe_name,
            token_address=token_address,
            anchor_block=anchor_block,
            stack=(),
        )
        addresses = tuple(sorted(members))
        sources = MappingProxyType(
            {
                address: tuple(sorted(members[address]))
                for address in addresses
            }
        )
        return FrozenUniverse(
            addresses=addresses,
            sources=sources,
            universe_hash=digest_universe(sources),
        )

    def _resolve_map(
        self,
        universe_name: str,
        *,
        token_address: str,
        anchor_block: int,
        stack: tuple[str, ...],
    ) -> dict[str, set[str]]:
        if universe_name in stack:
            path = " -> ".join((*stack, universe_name))
            raise ConfigError(f"universe selector cycle: {path}")
        try:
            selector = self.universes[universe_name]
        except KeyError as exc:
            raise ConfigError(f"unknown universe selector: {universe_name}") from exc

        next_stack = (*stack, universe_name)
        if selector.kind == "full_holders":
            full_output: dict[str, set[str]] = {}
            for member in self.holder_repository.holder_members(
                chain_id=self.chain_id,
                token_address=token_address,
                anchor_block=anchor_block,
            ):
                address = normalize_address(member.address)
                if not member.sources:
                    raise ConfigError(
                        f"holder repository returned no provenance for {address}"
                    )
                labels = {
                    f"full_holders:{universe_name}:{source}"
                    for source in member.sources
                    if source
                }
                if len(labels) != len(set(member.sources)):
                    raise ConfigError(
                        f"holder repository returned empty provenance for {address}"
                    )
                full_output.setdefault(address, set()).update(labels)
            for address in self.seed_holders.get(token_address, ()):
                full_output.setdefault(address, set()).add("full_holders:config_seed")
            return full_output

        if selector.kind == "explicit_list":
            return {
                normalize_address(address): {
                    f"explicit_list:{universe_name}:{selector.source}"
                }
                for address in self.explicit_list_loader(universe_name)
            }

        children = [
            self._resolve_map(
                child,
                token_address=token_address,
                anchor_block=anchor_block,
                stack=next_stack,
            )
            for child in selector.of
        ]
        if not children:
            # Pydantic rejects this shape; keep the runtime boundary fail-closed too.
            raise ConfigError(f"{selector.kind} universe {universe_name} has no children")

        if selector.kind == "union":
            union_output: dict[str, set[str]] = {}
            for child in children:
                for address, labels in child.items():
                    union_output.setdefault(address, set()).update(labels)
            return union_output

        if selector.kind == "intersect":
            shared = set(children[0])
            for child in children[1:]:
                shared.intersection_update(child)
            return {
                address: set().union(*(child[address] for child in children))
                for address in shared
            }

        raise ConfigError(f"unsupported universe selector kind: {selector.kind}")


__all__ = [
    "ClickHouseHolderUniverseRepository",
    "HolderUniverseRepository",
    "UniverseMember",
    "UniverseResolver",
]
