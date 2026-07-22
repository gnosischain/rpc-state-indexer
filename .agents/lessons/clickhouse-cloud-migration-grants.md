---
name: clickhouse-cloud-migration-grants
symptom: "Code 497 ACCESS_DENIED ... necessary to have the grant CREATE DATABASE/CREATE VIEW"
area: migrations
status: active
updated: 2026-07-21
---

## Symptom

`migrate` fails with `Code: 497 ACCESS_DENIED`, e.g. "necessary to have the grant
CREATE DATABASE ON <db>.*" — even though the database already exists — or later the same for
CREATE VIEW / DROP VIEW.

## Root cause

On ClickHouse Cloud the target DB is usually pre-created and the service user is scoped to
table DDL only (`CREATE TABLE, ALTER TABLE, INSERT, SELECT, ...`). But migration `000` runs
`CREATE DATABASE IF NOT EXISTS` (the grant is checked before the IF-NOT-EXISTS short-circuit),
and `007` runs `CREATE OR REPLACE VIEW` x19 (needs CREATE VIEW + DROP VIEW). The service user
lacks those three.

## Fix / correct pattern

Grant the three missing DDL privileges once, as an admin/`default` user:

```sql
GRANT CREATE DATABASE, CREATE VIEW, DROP VIEW ON <db>.* TO <service_user>;
```

Least-privilege alternative: run `migrate` as an admin user and keep the scoped service user
for the runtime daemon/jobs. Verify current grants with `SHOW GRANTS` (via a direct client;
the CLI hides the error — see [[cli-hides-real-db-error]]).

## How to avoid / detect

Before deploying, `SHOW GRANTS FOR <user>` and diff against the DDL the migrations use
(`grep -oiE 'CREATE (DATABASE|VIEW|TABLE)|DROP (VIEW|TABLE)' migrations/*.sql`).
