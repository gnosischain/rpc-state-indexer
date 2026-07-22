from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from operator import setitem
from typing import Any, cast

import pytest

from rpc_state_indexer.config.models import UniverseConfig
from rpc_state_indexer.core.universes import (
    ClickHouseHolderUniverseRepository,
    UniverseMember,
    UniverseResolver,
)
from rpc_state_indexer.errors import ConfigError
from rpc_state_indexer.storage.digests import digest_universe

TOKEN = "0x" + "99" * 20
A = "0x" + "11" * 20
B = "0x" + "22" * 20
C = "0x" + "33" * 20


@dataclass
class FakeHolders:
    members: tuple[UniverseMember, ...]
    calls: list[tuple[int, str, int]]

    def holder_members(
        self,
        *,
        chain_id: int,
        token_address: str,
        anchor_block: int,
    ) -> Iterable[UniverseMember]:
        self.calls.append((chain_id, token_address, anchor_block))
        return reversed(self.members)


def resolver(
    universes: Mapping[str, UniverseConfig],
    *,
    explicit: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[UniverseResolver, FakeHolders]:
    repository = FakeHolders(
        (
            UniverseMember(B, ("own_scan",)),
            UniverseMember(A.upper().replace("0X", "0x"), ("seed", "own_scan")),
        ),
        [],
    )
    values = explicit or {"watch": (B, C, C)}
    return (
        UniverseResolver(
            chain_id=100,
            universes=universes,
            holder_repository=repository,
            explicit_list_loader=lambda name: values[name],
        ),
        repository,
    )


def test_union_and_intersection_are_sorted_deduplicated_and_provenanced() -> None:
    universes = {
        "full": UniverseConfig(kind="full_holders"),
        "watch": UniverseConfig(
            kind="explicit_list", source="vendored/watch.csv"
        ),
        "either": UniverseConfig(kind="union", of=["watch", "full"]),
        "both": UniverseConfig(kind="intersect", of=["full", "watch"]),
    }
    subject, repository = resolver(universes)

    union = subject.resolve("either", token_address=TOKEN, anchor_block=123)
    intersection = subject.resolve("both", token_address=TOKEN, anchor_block=123)

    assert union.addresses == (A, B, C)
    assert intersection.addresses == (B,)
    assert union.sources[B] == (
        "explicit_list:watch:vendored/watch.csv",
        "full_holders:full:own_scan",
    )
    assert intersection.sources[B] == union.sources[B]
    assert union.universe_hash == digest_universe(union.sources)
    assert repository.calls == [(100, TOKEN, 123), (100, TOKEN, 123)]
    with pytest.raises(TypeError):
        setitem(
            cast(MutableMapping[str, tuple[str, ...]], union.sources),
            A,
            ("mutated",),
        )


def test_universe_digest_is_independent_of_repository_order() -> None:
    universes = {"full": UniverseConfig(kind="full_holders")}
    first, _ = resolver(universes)
    second, _ = resolver(universes)

    left = first.resolve("full", token_address=TOKEN, anchor_block=1)
    right = second.resolve("full", token_address=TOKEN, anchor_block=1)

    assert left == right


def test_configured_seed_holders_are_part_of_full_history_universe() -> None:
    universes = {"full": UniverseConfig(kind="full_holders")}
    repository = FakeHolders((), [])
    subject = UniverseResolver(
        chain_id=100,
        universes=universes,
        holder_repository=repository,
        explicit_list_loader=lambda _name: (),
        seed_holders={TOKEN: (C,)},
    )

    result = subject.resolve("full", token_address=TOKEN, anchor_block=123)

    assert result.addresses == (C,)
    assert result.sources[C] == ("full_holders:config_seed",)


def test_recursive_cycle_fails_with_the_reference_path() -> None:
    universes = {
        "full": UniverseConfig(kind="full_holders"),
        "a": UniverseConfig(kind="union", of=["b", "full"]),
        "b": UniverseConfig(kind="intersect", of=["a", "full"]),
    }
    subject, _ = resolver(universes)

    with pytest.raises(ConfigError, match=r"a -> b -> a"):
        subject.resolve("a", token_address=TOKEN, anchor_block=1)


class FakeQueryRepository:
    def __init__(self) -> None:
        self.sql = ""
        self.parameters: Mapping[str, Any] = {}

    def query_rows(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.sql = sql
        self.parameters = parameters or {}
        return [{"holder_address": A, "member_sources": ["own_scan"]}]


def test_clickhouse_adapter_pins_token_and_anchor() -> None:
    query_repository = FakeQueryRepository()
    adapter = ClickHouseHolderUniverseRepository(query_repository)

    members = adapter.holder_members(
        chain_id=100,
        token_address=TOKEN,
        anchor_block=456,
    )

    assert members == (UniverseMember(A, ("own_scan",)),)
    assert "first_seen_block <= {anchor_block:UInt64}" in query_repository.sql
    assert query_repository.parameters == {
        "chain_id": 100,
        "token_address": TOKEN,
        "anchor_block": 456,
    }
