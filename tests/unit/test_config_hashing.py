from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel

from rpc_state_indexer.config.loader import (
    HASH_NEUTRAL_WHEN_EMPTY_TARGET_FIELDS,
    Catalog,
    load_catalog,
)
from rpc_state_indexer.config.models import (
    PoolConfig,
    TokenConfig,
    discovered_token_config,
)

ROOT = Path(__file__).parents[2]

#: EURe v1 — the ledger that shares balances with v2 and carries universe_aliases.
EURE_V1 = "0xcb444e90d8198415266c6a2724b7900fb12fc56e"


def _catalog() -> Catalog:
    return load_catalog(ROOT / "config", "gnosis")


def test_operational_batch_sizes_do_not_change_target_config_hash() -> None:
    catalog = _catalog()
    job = catalog.jobs["daily_curated_balances"]
    token = catalog.token_targets(job)[0]
    original = catalog.target_config_hash(job, token)

    multicall = catalog.chain.multicall3.model_copy(
        update={"default_batch_size": 17}
    )
    legacy = catalog.chain.legacy_execution.model_copy(
        update={"default_batch_size": 19}
    )
    changed_chain = catalog.chain.model_copy(
        update={"multicall3": multicall, "legacy_execution": legacy}
    )
    changed = replace(catalog, chain=changed_chain)

    assert changed.target_config_hash(job, token) == original


def test_cadence_does_not_change_target_config_hash() -> None:
    catalog = _catalog()
    job = catalog.jobs["daily_curated_balances"]
    token = catalog.token_targets(job)[0]
    manual = job.model_copy(update={"cadence": "manual"})

    assert catalog.target_config_hash(manual, token) == catalog.target_config_hash(
        job, token
    )


def test_static_selector_hash_is_unaffected_by_the_discovered_flag() -> None:
    """A false `discovered` flag must not enter the hash.

    Hashing it would change config_hash for every already-published static job, dropping
    their history out of the eligible views for what is a pure no-op.
    """

    catalog = _catalog()
    for job_name in (
        "daily_curated_balances",
        "daily_token_supply",
        "daily_atokens_full",
    ):
        job = catalog.jobs[job_name]
        effective = catalog.target_effective_config(job, catalog.token_targets(job)[0])
        assert "discovered" not in effective["job"]["token_selector"], job_name


def test_discovered_selector_is_hashed() -> None:
    from rpc_state_indexer.config.models import discovered_token_config

    catalog = load_catalog(ROOT / "config", "ethereum")
    job = catalog.jobs["daily_treasury"]
    token = discovered_token_config("0x00000000000000000000000000000000000005aa", 1)
    effective = catalog.target_effective_config(job, token)

    assert effective["job"]["token_selector"]["discovered"] is True


def test_universe_membership_file_changes_target_config_hash(tmp_path: Path) -> None:
    catalog = _catalog()
    chain_root = tmp_path / "gnosis"
    chain_root.mkdir(parents=True)
    vendored = chain_root / "vendored"
    vendored.mkdir()
    source = vendored / "supply_probe.csv"
    source.write_text("address\n0x1111111111111111111111111111111111111111\n")
    probe = catalog.universes["supply_probe"].model_copy(
        update={"source": "vendored/supply_probe.csv"}
    )
    changed = replace(
        catalog,
        root=tmp_path,
        universes={**catalog.universes, "supply_probe": probe},
    )
    job = changed.jobs["daily_token_supply"]
    token = changed.token_targets(job)[0]
    first = changed.target_config_hash(job, token)

    source.write_text("address\n0x2222222222222222222222222222222222222222\n")

    assert changed.target_config_hash(job, token) != first


def test_empty_universe_aliases_stay_out_of_the_hash() -> None:
    """An empty `universe_aliases` must not enter the hash (same rule as `discovered`).

    fa80947 (2026-09-08) hashed `[]` for every token, which re-hashed every token target
    on both chains and hid ~3.4M published rows from the eligible views for a no-op.
    """

    catalog = _catalog()
    job = catalog.jobs["daily_token_supply"]
    token = next(t for t in catalog.token_targets(job) if not t.universe_aliases)
    effective = catalog.target_effective_config(job, token)

    assert "universe_aliases" not in effective["target"]


def test_non_empty_universe_aliases_are_hashed() -> None:
    catalog = _catalog()
    job = catalog.jobs["daily_token_supply"]
    eure_v1 = catalog.tokens[EURE_V1]
    assert eure_v1.universe_aliases, "fixture drifted: EURe v1 should alias the v2 ledger"
    without = eure_v1.model_copy(update={"universe_aliases": []})

    effective = catalog.target_effective_config(job, eure_v1)
    assert effective["target"]["universe_aliases"] == eure_v1.universe_aliases
    assert catalog.target_config_hash(job, eure_v1) != catalog.target_config_hash(
        job, without
    )


#: Every field each target model had when the hashes in PRODUCTION_HASHES were published
#: (models.py at fa80947~1). Their values, defaults included, are inside every published
#: hash, so they stay hashed: removing one would re-hash everything, just as adding one did.
HASHED_TARGET_FIELDS: dict[type[BaseModel], frozenset[str]] = {
    TokenConfig: frozenset(
        {
            "address", "symbol", "decimals", "token_class", "deployment_block",
            "date_start", "date_end", "enabled", "zero_address_role", "balance_function",
            "supply_functions", "discovery_events", "seed_holders", "index_source",
        }
    ),
    PoolConfig: frozenset(
        {
            "address", "name", "pool_class", "deployment_block", "date_start", "date_end",
            "enabled", "assets", "pool_id", "tick_spacing", "fee",
        }
    ),
}


@pytest.mark.parametrize("model", [TokenConfig, PoolConfig], ids=["token", "pool"])
def test_every_new_target_field_is_hash_neutral_when_empty(model: type[BaseModel]) -> None:
    """A field added to a target model must be listed as hash-neutral when empty.

    Otherwise its default enters every existing target's hash on the next deploy and hides
    all published history from the v_*_published views (what fa80947 did). If the new field
    really changes what every existing target measures, that is a controlled reindex
    (docs/runbook.md §14): add it to HASHED_TARGET_FIELDS in the same change instead.
    """

    new_fields = (
        set(model.model_fields)
        - HASHED_TARGET_FIELDS[model]
        - set(HASH_NEUTRAL_WHEN_EMPTY_TARGET_FIELDS)
    )
    assert not new_fields, (
        f"{model.__name__} gained {sorted(new_fields)}: add each to "
        "HASH_NEUTRAL_WHEN_EMPTY_TARGET_FIELDS in rpc_state_indexer/config/loader.py"
    )


#: Hashes carried by PUBLISHED production rows (census_publications, checked against the
#: warehouse on 2026-09-25). If one of these fails, a change re-hashed existing targets
#: and would hide their published history from every v_*_published view. Either make the
#: change hash-neutral (see HASH_NEUTRAL_WHEN_EMPTY_TARGET_FIELDS) or treat it as a
#: controlled reindex (docs/runbook.md §14) and update the pin deliberately.
PRODUCTION_HASHES: list[tuple[str, str, str, str, int | None, str]] = [
    (
        "ethereum",
        "daily_treasury",
        "catalog",
        "0x6810e776880c02933d47db1b9fc05908e5386b96",
        None,
        "83a6b5b3d0677caf14b214ca12aedba8ccdbd58213c857bec294dadfce0adc3f",
    ),
    (
        "gnosis",
        "daily_treasury",
        "catalog",
        "0x9c58bacc331c9aa871afd802db6379a98e80cedb",
        None,
        "f9ae93a54f137425ea286f97245d07eeb18fde2805f8030492e4eafaaf82c7ae",
    ),
    (
        "ethereum",
        "daily_treasury",
        "discovered",
        "0x0006a558c49b79aaee10144fad1616cd791a5405",
        15825623,
        "6f0d9a7748bcc19d4feef506f18f94ae8488600ee14b9c8681836fdd5f718f85",
    ),
    (
        "gnosis",
        "daily_treasury",
        "discovered",
        "0x0811e451447d5819976a95a02f130c3b00d59346",
        21953548,
        "59426641b16ab52180de2ae1f795541133875aebfd408cbe506e03ba4442d97f",
    ),
    (
        "ethereum",
        "daily_gno_supply_wallets",
        "catalog",
        "0x6810e776880c02933d47db1b9fc05908e5386b96",
        None,
        "405a974c1507b803b50691561590fd50a774f0e001faa6145f2881311fdffe70",
    ),
    (
        "gnosis",
        "daily_token_supply",
        "catalog",
        "0x9c58bacc331c9aa871afd802db6379a98e80cedb",
        None,
        "f1a18f2d088d18bddbed45da5065a5cdd140eacd77be58d811678b2c0c2e21f3",
    ),
    (
        "gnosis",
        "daily_gno_supply_scalar",
        "catalog",
        "0x9c58bacc331c9aa871afd802db6379a98e80cedb",
        None,
        "ea69e442ed42a7a17079b19fc239e03458e84174c1e1d277b6e63c16ba19f47a",
    ),
    (
        "gnosis",
        "daily_atokens_full",
        "catalog",
        "0xd0dd6cef72143e22cced4867eb0d5f2328715533",
        None,
        "81384181769002d570e67b939064afdf90939f9aacbef0b8eddbefad0c1dbf46",
    ),
    (
        "gnosis",
        "daily_pool_reserves",
        "pool",
        "0x001202ea55f2dda9e2d0c2a4eb2306ebd269fcef",
        None,
        "f28fb2945e9aa340b154b071c9e5756d9becd2e1e4c209a88eafeebbb605944c",
    ),
    (
        "gnosis",
        "daily_cl_liquidity",
        "pool",
        "0x00cd063c0d8160614b4239d586e681492058f418",
        None,
        "7ba488919c38380ae10a07f5cdcbf450ab0ebb44c3ecd7d07c0dc4983a860254",
    ),
]


@pytest.mark.parametrize(
    ("chain", "job_name", "source", "address", "first_seen_block", "expected"),
    PRODUCTION_HASHES,
    ids=[f"{row[0]}-{row[1]}-{row[3][:10]}" for row in PRODUCTION_HASHES],
)
def test_published_production_hashes_are_stable(
    chain: str,
    job_name: str,
    source: str,
    address: str,
    first_seen_block: int | None,
    expected: str,
) -> None:
    catalog = load_catalog(ROOT / "config", chain)
    job = catalog.jobs[job_name]
    target: TokenConfig | PoolConfig
    if source == "pool":
        target = next(
            pool for pool in catalog.pool_targets(job) if pool.address.lower() == address
        )
    elif source == "discovered":
        assert first_seen_block is not None
        target = discovered_token_config(address, first_seen_block)
    else:
        target = catalog.tokens[address]

    assert catalog.target_config_hash(job, target) == expected
