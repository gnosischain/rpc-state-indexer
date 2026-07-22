from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from rpc_state_indexer.storage.clickhouse import (
    ClickHouseConnectionSettings,
    create_clickhouse_client,
)
from rpc_state_indexer.storage.migrations import (
    MigrationError,
    MigrationRunner,
    split_sql_statements,
)

MIGRATIONS = Path(__file__).parents[3] / "migrations"


def test_statement_splitter_preserves_semicolons_in_literals() -> None:
    statements = split_sql_statements(
        """
        -- comment with ;
        CREATE TABLE example (`value` String) ENGINE = Memory;
        INSERT INTO example VALUES ('a;b');
        /* another ; comment */ SELECT 1;
        """
    )

    assert len(statements) == 3
    assert "'a;b'" in statements[1]


def test_statement_splitter_rejects_unterminated_quote() -> None:
    with pytest.raises(MigrationError, match="unterminated quoted string"):
        split_sql_statements("SELECT 'broken;")


@pytest.mark.integration
@pytest.mark.skipif(
    not os.getenv("CLICKHOUSE_TEST_HOST"),
    reason="CLICKHOUSE_TEST_HOST is not configured",
)
def test_real_clickhouse_migrations_are_idempotent() -> None:
    database = f"rpc_indexer_test_{uuid4().hex}"
    settings = ClickHouseConnectionSettings(
        host=os.environ["CLICKHOUSE_TEST_HOST"],
        port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8443")),
        username=os.getenv("CLICKHOUSE_TEST_USER", "default"),
        password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""),
        database=database,
        secure=os.getenv("CLICKHOUSE_TEST_SECURE", "true").lower() == "true",
        verify=os.getenv("CLICKHOUSE_TEST_VERIFY", "true").lower() == "true",
    )
    client = create_clickhouse_client(settings, connect_to_database=False)

    try:
        first = MigrationRunner(client, database, MIGRATIONS).apply()
        second = MigrationRunner(client, database, MIGRATIONS).apply()

        assert first
        assert all(outcome.status == "applied" for outcome in first)
        assert all(outcome.status == "skipped" for outcome in second)

        result = client.query(
            f"SELECT count(), uniqExact(name) FROM {database}.migrations"
        )
        assert result.result_rows[0][0] == result.result_rows[0][1]
        assert result.result_rows[0][0] == len(first)

        # Compile the view graph and force the publication gate through query
        # analysis.  This catches schema/view drift even when every CREATE succeeds.
        client.query(f"SELECT * FROM {database}.v_publications_eligible LIMIT 0")
        client.query(f"SELECT * FROM {database}.v_coverage_calendar LIMIT 0")
        columns = client.query(
            "SELECT name FROM system.columns "
            f"WHERE database = '{database}' AND table = 'v_config_registry_current'"
        )
        assert "coverage_end" in {row[0] for row in columns.result_rows}
    finally:
        client.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        client.close()
