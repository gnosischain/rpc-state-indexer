from dataclasses import replace
from pathlib import Path

from rpc_state_indexer.config.loader import Catalog, load_catalog

ROOT = Path(__file__).parents[2]


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
