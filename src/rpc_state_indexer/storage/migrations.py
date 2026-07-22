"""Checksum-verified ClickHouse migration runner."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_DATABASE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MIGRATION_RE = re.compile(r"^(?P<number>[0-9]{3})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """Base class for migration failures."""


class MigrationChecksumError(MigrationError):
    """An already-applied migration changed on disk."""


@dataclass(frozen=True, slots=True)
class Migration:
    number: int
    name: str
    path: Path
    checksum: str
    sql: str


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    name: str
    checksum: str
    status: Literal["applied", "skipped"]


def validate_database_name(database: str) -> str:
    if not _DATABASE_RE.fullmatch(database):
        raise MigrationError(f"invalid ClickHouse database identifier: {database!r}")
    return database


def discover_migrations(directory: Path) -> list[Migration]:
    if not directory.is_dir():
        raise MigrationError(f"migration directory does not exist: {directory}")

    migrations: list[Migration] = []
    numbers: dict[int, str] = {}

    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix != ".sql":
            continue
        match = _MIGRATION_RE.fullmatch(path.name)
        if match is None:
            raise MigrationError(
                f"migration filename must match NNN_lowercase_name.sql: {path.name}"
            )
        number = int(match.group("number"))
        if number in numbers:
            raise MigrationError(
                f"duplicate migration number {number:03d}: "
                f"{numbers[number]} and {path.name}"
            )
        numbers[number] = path.name
        raw = path.read_bytes()
        try:
            sql = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"migration is not UTF-8: {path.name}") from exc
        migrations.append(
            Migration(
                number=number,
                name=path.name,
                path=path,
                checksum=hashlib.sha256(raw).hexdigest(),
                sql=sql,
            )
        )

    if not migrations:
        raise MigrationError(f"no SQL migrations found in {directory}")
    if migrations[0].number != 0:
        raise MigrationError("the first migration must be numbered 000")
    return migrations


def render_migration(sql: str, database: str) -> str:
    rendered = sql.replace("{{database}}", validate_database_name(database))
    if "{{" in rendered or "}}" in rendered:
        raise MigrationError("unknown or malformed template placeholder in migration")
    return rendered


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL while respecting quoted strings and SQL comments.

    The migration files deliberately avoid stored procedures and dollar-quoted strings;
    this parser covers ClickHouse DDL, views, comments, and semicolons inside quoted
    literals without relying on brittle ``str.split(';')`` behavior.
    """

    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0

    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""

        if line_comment:
            if char == "\n":
                line_comment = False
                buffer.append("\n")
            index += 1
            continue

        if block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue

        if quote is not None:
            buffer.append(char)
            if char == "\\" and following:
                buffer.append(following)
                index += 2
                continue
            if char == quote:
                if following == quote:
                    buffer.append(following)
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char == "-" and following == "-":
            line_comment = True
            index += 2
            continue
        if char == "/" and following == "*":
            block_comment = True
            index += 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer.clear()
            index += 1
            continue

        buffer.append(char)
        index += 1

    if quote is not None:
        raise MigrationError("unterminated quoted string in migration")
    if block_comment:
        raise MigrationError("unterminated block comment in migration")

    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    return statements


class MigrationRunner:
    """Apply immutable SQL migrations and verify their recorded checksums."""

    def __init__(self, client: Any, database: str, directory: str | Path) -> None:
        self.client = client
        self.database = validate_database_name(database)
        self.directory = Path(directory)

    @property
    def ledger_table(self) -> str:
        return f"{self.database}.migrations"

    def apply(self) -> list[MigrationOutcome]:
        migrations = discover_migrations(self.directory)
        bootstrapped: set[str] = set()
        if not self._ledger_exists():
            self._execute(migrations[0])
            self._record(migrations[0])
            bootstrapped.add(migrations[0].name)

        applied = self._load_applied()
        outcomes: list[MigrationOutcome] = []

        for migration in migrations:
            recorded_checksum = applied.get(migration.name)
            if recorded_checksum is not None:
                if recorded_checksum != migration.checksum:
                    raise MigrationChecksumError(
                        f"applied migration changed: {migration.name}; "
                        f"database={recorded_checksum}, file={migration.checksum}"
                    )
                outcomes.append(
                    MigrationOutcome(
                        migration.name,
                        migration.checksum,
                        "applied" if migration.name in bootstrapped else "skipped",
                    )
                )
                continue

            self._execute(migration)
            self._record(migration)
            applied[migration.name] = migration.checksum
            outcomes.append(
                MigrationOutcome(migration.name, migration.checksum, "applied")
            )

        return outcomes

    def _ledger_exists(self) -> bool:
        result = self.client.query(
            "SELECT count() FROM system.tables "
            f"WHERE database = '{self.database}' AND name = 'migrations'"
        )
        rows = getattr(result, "result_rows", [])
        return bool(rows and int(rows[0][0]) == 1)

    def _load_applied(self) -> dict[str, str]:
        result = self.client.query(
            f"SELECT name, checksum FROM {self.ledger_table} "
            "ORDER BY name, applied_at"
        )
        applied: dict[str, str] = {}
        for name, checksum in getattr(result, "result_rows", []):
            normalized_checksum = (
                checksum.decode("ascii") if isinstance(checksum, bytes) else str(checksum)
            )
            existing = applied.get(str(name))
            if existing is not None and existing != normalized_checksum:
                raise MigrationChecksumError(
                    f"migration ledger has conflicting checksums for {name}"
                )
            applied[str(name)] = normalized_checksum
        return applied

    def _execute(self, migration: Migration) -> None:
        rendered = render_migration(migration.sql, self.database)
        statements = split_sql_statements(rendered)
        if not statements:
            raise MigrationError(f"migration contains no statements: {migration.name}")
        for statement in statements:
            self.client.command(statement)

    def _record(self, migration: Migration) -> None:
        # Both values are constrained to safe filename/hex grammars.  Keeping the
        # ledger insert independent of driver-specific parameter syntax also makes the
        # runner usable during database bootstrap and in small fake-client tests.
        statement = (
            f"INSERT INTO {self.ledger_table} (name, checksum) VALUES "
            f"('{migration.name}', '{migration.checksum}')"
        )
        try:
            self.client.command(statement)
        except Exception:
            # An ambiguous network failure may occur after ClickHouse committed.  Treat
            # it as success only when the exact checksum can be read back.
            recorded = self._load_applied().get(migration.name)
            if recorded != migration.checksum:
                raise
