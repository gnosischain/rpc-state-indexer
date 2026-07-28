from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from rpc_state_indexer.config.loader import load_catalog
from rpc_state_indexer.config.models import (
    JobConfig,
    TokenSelector,
    discovered_token_config,
)
from rpc_state_indexer.domain import IntegrityMode
from rpc_state_indexer.service import IndexerService
from rpc_state_indexer.settings import RuntimeSettings

ROOT = Path(__file__).parents[2]

SPAM = "0x00000000000000000000000000000000000005aa"
GNO = "0x6810e776880c02933d47db1b9fc05908e5386b96"


def test_token_selector_discovered_is_exclusive() -> None:
    assert TokenSelector(discovered=True).discovered
    with pytest.raises(ValueError):
        TokenSelector(discovered=True, all_enabled=True)
    with pytest.raises(ValueError):
        TokenSelector()


def test_discovered_token_config_synthesis() -> None:
    token = discovered_token_config(SPAM, 123)
    assert token.address == SPAM
    assert token.symbol == SPAM
    assert token.deployment_block == 123
    assert token.token_class == "standard_erc20"
    assert token.balance_function == "balanceOf"


def test_discovered_job_requires_scoped_integrity() -> None:
    with pytest.raises(ValueError):
        JobConfig(
            name="bad",
            target_kind="tokens",
            token_selector=TokenSelector(discovered=True),
            universe="treasury",
            integrity_mode=IntegrityMode.FULL_SUPPLY,
        )
        # full_supply requires full_holders at catalog level; scoped enforcement is
        # asserted through the catalog below.


def test_catalogs_load_with_discovered_jobs() -> None:
    for chain in ("gnosis", "ethereum"):
        catalog = load_catalog(ROOT / "config", chain)
        job = catalog.jobs["daily_treasury"]
        selector = job.token_selector
        assert selector is not None and selector.discovered
        # Static resolution is deliberately empty; runtime resolves candidates.
        assert catalog.token_targets(job) == ()
        assert catalog.sweeps["treasury_interactions"].universe == "treasury"
        wallets = catalog.explicit_addresses("treasury")
        assert len(wallets) == 23


def test_catalog_rejects_discovered_job_without_scoped_integrity(
    tmp_path: Path,
) -> None:
    source = ROOT / "config"
    target = tmp_path / "config"
    import shutil

    shutil.copytree(source, target)
    jobs = target / "ethereum" / "jobs.yaml"
    jobs.write_text(
        jobs.read_text().replace("integrity_mode: scoped", "integrity_mode: full_supply")
    )
    with pytest.raises(ValidationError, match="scoped integrity"):
        load_catalog(target, "ethereum")


class FakeRepository:
    def __init__(
        self,
        candidates: list[tuple[str, int]],
        quarantined: frozenset[str] = frozenset(),
    ) -> None:
        self.candidates = candidates
        self.quarantined = quarantined
        self.registered: list[dict[str, Any]] = []
        self.candidate_calls = 0

    def discovered_token_candidates(self, chain_id: int) -> list[tuple[str, int]]:
        del chain_id
        self.candidate_calls += 1
        return self.candidates

    def quarantined_token_targets(
        self, chain_id: int, job_name: str, threshold: int
    ) -> frozenset[str]:
        del chain_id, job_name, threshold
        return self.quarantined

    def register_configs(self, rows: Any) -> int:
        materialized = list(rows)
        self.registered.extend(materialized)
        return len(materialized)


def make_service(repository: FakeRepository, chain: str = "ethereum") -> IndexerService:
    service = IndexerService(RuntimeSettings(CHAIN=chain), "census")
    service.catalog = load_catalog(ROOT / "config", chain)
    service.repository = cast(Any, repository)
    service.runtime = cast(Any, object())
    return service


def treasury_job(service: IndexerService) -> JobConfig:
    assert service.catalog is not None
    return service.catalog.jobs["daily_treasury"]


def test_token_targets_synthesizes_and_registers_discovered() -> None:
    repository = FakeRepository([(SPAM, 500)])
    service = make_service(repository)
    targets = service._token_targets(treasury_job(service))

    assert [token.address for token in targets] == [SPAM]
    assert targets[0].deployment_block == 500
    registered = {row["target_address"] for row in repository.registered}
    assert registered == {SPAM}
    assert all(row["integrity_mode"] == "scoped" for row in repository.registered)


def test_token_targets_prefers_curated_metadata() -> None:
    repository = FakeRepository([(GNO, 11_500_000), (SPAM, 500)])
    service = make_service(repository)
    targets = service._token_targets(treasury_job(service))

    by_address = {token.address: token for token in targets}
    assert set(by_address) == {GNO, SPAM}
    # GNO resolves to the curated entry (real symbol/decimals), not a synthesized one.
    assert by_address[GNO].symbol == "GNO"
    assert by_address[GNO].decimals == 18
    assert by_address[SPAM].symbol == SPAM


def test_token_targets_excludes_quarantined_and_stays_deterministic() -> None:
    repository = FakeRepository(
        [(SPAM, 500), (GNO, 100)], quarantined=frozenset({SPAM})
    )
    service = make_service(repository)
    targets = service._token_targets(treasury_job(service))

    assert [token.address for token in targets] == [GNO]
    # Quarantine withholds measurement, not config identity: the target stays
    # registered so a recovery needs no re-registration, and registry rows alone
    # publish nothing.
    registered = {row["target_address"] for row in repository.registered}
    assert registered == {SPAM, GNO}


def test_discovered_targets_resolve_and_register_once_per_process() -> None:
    """A multi-date backfill must not re-register the same targets on every date."""

    repository = FakeRepository([(SPAM, 500), (GNO, 100)])
    service = make_service(repository)
    job = treasury_job(service)

    first = service._token_targets(job)
    second = service._token_targets(job)

    assert first == second
    assert len(repository.registered) == 2  # one row per target, registered once
    assert repository.candidate_calls == 1


def test_quarantine_is_rechecked_after_caching() -> None:
    """A target that starts failing mid-backfill drops out for the remaining dates."""

    repository = FakeRepository([(SPAM, 500), (GNO, 100)])
    service = make_service(repository)
    job = treasury_job(service)

    assert {t.address for t in service._token_targets(job)} == {SPAM, GNO}
    repository.quarantined = frozenset({SPAM})
    assert [t.address for t in service._token_targets(job)] == [GNO]


def test_token_targets_static_jobs_unchanged() -> None:
    repository = FakeRepository([])
    service = make_service(repository, chain="gnosis")
    assert service.catalog is not None
    job = service.catalog.jobs["daily_curated_balances"]
    targets = service._token_targets(job)
    assert targets == service.catalog.token_targets(job)
    assert repository.registered == []
