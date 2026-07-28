from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from uuid import UUID

from rpc_state_indexer.storage.repositories import TABLE_COLUMNS, AttemptScope

ROOT = Path(__file__).parents[2]


def test_attempt_scoped_reads_use_the_sort_key_prefix() -> None:
    """No read may filter on attempt_id alone.

    Every attempt/observation table is ordered by
    (chain_id, job_name, <target>_address, snapshot_date, attempt_id, ...). Filtering on
    attempt_id alone cannot use that prefix, so ClickHouse falls back to a full scan —
    which, with FINAL over a 200M-row table, cost ~9s per read-back instead of ~60ms and
    degraded without bound as history grew.
    """

    source = (
        ROOT / "src/rpc_state_indexer/storage/repositories.py"
    ).read_text()
    offenders = re.findall(r"WHERE\s+attempt_id\s*=\s*\{\{attempt_id:UUID\}\}", source)
    assert not offenders, (
        f"{len(offenders)} query(s) filter on attempt_id alone; "
        "use AttemptScope.predicate() so the sort-key prefix prunes"
    )


def test_attempt_scope_predicate_and_parameters_agree() -> None:
    predicate = AttemptScope.predicate("token_address")
    placeholders = set(re.findall(r"\{(\w+):", predicate))
    scope = AttemptScope(
        chain_id=100,
        job_name="daily_treasury",
        target_address="0x" + "11" * 20,
        snapshot_date=date(2026, 7, 27),
        attempt_id=UUID(int=1),
    )

    assert placeholders == set(scope.parameters())
    # The prefix must lead with the sort key, not just tack attempt_id on.
    assert predicate.startswith("chain_id = {chain_id:UInt64}")
    assert "token_address = {target_address:String}" in predicate


def test_repository_column_contract_matches_table_migrations() -> None:
    ddl = "\n".join(
        path.read_text()
        for path in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql"))
    )

    for table, repository_columns in TABLE_COLUMNS.items():
        match = re.search(
            rf"CREATE TABLE IF NOT EXISTS \{{\{{database\}}\}}\.{table}"
            r"\s*\((.*?)\)\s*ENGINE",
            ddl,
            re.DOTALL,
        )
        assert match is not None, f"missing DDL for repository table {table}"
        ddl_columns = tuple(
            line.split(maxsplit=1)[0].rstrip(",")
            for line in (raw.strip() for raw in match.group(1).splitlines())
            if line and not line.startswith(("--", "insert_version"))
        )
        assert ddl_columns == repository_columns, table

