from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from rpc_state_indexer import cli, service
from rpc_state_indexer.cli import app

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


def test_module_help_lists_operational_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    for command in (
        "validate-config",
        "migrate",
        "status",
        "validate",
        "probe",
        "discover",
        "census",
        "backfill",
        "densify",
        "bench",
        "daemon",
    ):
        assert command in result.output


def test_validate_config_is_offline_and_accepts_explicit_roots() -> None:
    result = runner.invoke(
        app,
        [
            "validate-config",
            "--chain",
            "gnosis",
            "--config-root",
            str(ROOT / "config"),
            "--abi-root",
            str(ROOT / "abis"),
        ],
        env={"RPC_URLS": "", "CLICKHOUSE_HOST": ""},
    )

    assert result.exit_code == 0, result.output
    assert "valid: chain=gnosis chain_id=100" in result.output


def test_unwired_execution_command_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_SERVICE_MODULE",
        "rpc_state_indexer.__definitely_missing_service",
    )
    result = runner.invoke(app, ["bench"])

    assert result.exit_code == 1
    assert "is not available yet" in result.output


def test_operational_validate_exits_nonzero_on_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_read_clickhouse_row",
        lambda settings, query: {
            "anchor_conflicts": 1,
            "publication_conflicts": 0,
            "transfer_log_conflicts": 0,
            "unfinished_attempts": 0,
            "unrepaired_failed_attempts": 0,
            "unresolved_errors": 0,
        },
    )

    result = runner.invoke(app, ["validate"])

    assert result.exit_code == 1
    assert "operational validation failed: anchor_conflicts" in result.output


def test_service_errors_keep_their_safe_actionable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_bench(**_kwargs: object) -> None:
        raise service.ServiceError("no safe single-batch size passed")

    monkeypatch.setattr(service, "run_bench", fail_bench)

    result = runner.invoke(app, ["bench"])

    assert result.exit_code == 1
    assert "bench failed: no safe single-batch size passed" in result.output
