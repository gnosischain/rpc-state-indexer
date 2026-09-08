import pytest

from rpc_state_indexer.settings import RuntimeSettings


def test_census_target_concurrency_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CENSUS_TARGET_CONCURRENCY", raising=False)
    assert RuntimeSettings().census_target_concurrency == 16
    monkeypatch.setenv("CENSUS_TARGET_CONCURRENCY", "32")
    assert RuntimeSettings().census_target_concurrency == 32


def test_multicall_max_parallel_batches_unset_by_default_and_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MULTICALL_MAX_PARALLEL_BATCHES", raising=False)
    assert RuntimeSettings().multicall_max_parallel_batches is None
    monkeypatch.setenv("MULTICALL_MAX_PARALLEL_BATCHES", "24")
    assert RuntimeSettings().multicall_max_parallel_batches == 24
    monkeypatch.setenv("MULTICALL_MAX_PARALLEL_BATCHES", "0")
    with pytest.raises(ValueError):
        RuntimeSettings()


def test_archive_probe_floor_raises_the_probe_block_only_upwards() -> None:
    from rpc_state_indexer.service import archive_probe_block

    assert archive_probe_block(11_173_937, None) == 11_173_937
    assert archive_probe_block(11_173_937, 20_000_000) == 20_000_000
    assert archive_probe_block(25_000_000, 20_000_000) == 25_000_000


def test_archive_probe_floor_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from rpc_state_indexer.settings import RuntimeSettings

    monkeypatch.setenv("ARCHIVE_PROBE_FLOOR_BLOCK", "20000000")
    assert RuntimeSettings().archive_probe_floor_block == 20_000_000
    monkeypatch.delenv("ARCHIVE_PROBE_FLOOR_BLOCK")
    assert RuntimeSettings().archive_probe_floor_block is None
