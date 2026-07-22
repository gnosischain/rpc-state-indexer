---
name: cli-hides-real-db-error
symptom: "error: migration failed (DatabaseError)" with no detail; any opaque CLI DB error
area: storage
status: active
updated: 2026-07-21
---

## Symptom

A storage command fails with a generic, detail-free message such as
`error: migration failed (DatabaseError)`. The real ClickHouse error code/message is not
shown, so you cannot tell whether it is auth, privileges, or SQL.

## Root cause

`cli.py` deliberately catches broad exceptions and prints only the class name to avoid
leaking secrets/URLs (e.g. `migrate` at `_fail(f"migration failed ({type(exc).__name__})")`).
A single `DatabaseError` can mean very different things (516 auth, 497 access-denied, 184
illegal aggregation, 60 unknown table...).

## Fix / correct pattern

Reproduce the operation against ClickHouse directly to see the real server message.
Parse `.env` (strip surrounding quotes from the password), build the app's
`ClickHouseConnectionSettings` + `create_clickhouse_client(connect_to_database=False)`, and
run the failing path (e.g. `MigrationRunner(...).apply()`) so the underlying
`clickhouse_connect` exception (with `Code: NNN ...`) prints. `SHOW GRANTS`,
`SELECT currentUser(), version()`, and `DESCRIBE`/`SHOW CREATE` are the fast next probes.

## How to avoid / detect

Keep a small read-only probe script handy; the real error code points straight at the fix:
516 -> [[env-password-quoting]], 497 -> [[clickhouse-cloud-migration-grants]],
184/60 in a view -> [[clickhouse-analyzer-view-sql]].
