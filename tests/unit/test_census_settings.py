import pytest

from rpc_state_indexer.settings import RuntimeSettings


def test_census_target_concurrency_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CENSUS_TARGET_CONCURRENCY", raising=False)
    assert RuntimeSettings().census_target_concurrency == 16
    monkeypatch.setenv("CENSUS_TARGET_CONCURRENCY", "32")
    assert RuntimeSettings().census_target_concurrency == 32
