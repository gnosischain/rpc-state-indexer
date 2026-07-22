from __future__ import annotations

import re
from pathlib import Path

from rpc_state_indexer.storage.repositories import TABLE_COLUMNS

ROOT = Path(__file__).parents[2]


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

