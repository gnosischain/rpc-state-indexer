---
name: deploy-run-sequence
type: ops
updated: 2026-07-21
---

Verified deployment/run path (via Docker Compose profiles; the image is the deploy
artifact). Run write steps **sequentially** — see [[single-writer-heartbeat]].

1. `docker compose build migrations jobs daemon` — **name the services**; a bare
   `docker compose build` is a no-op here because all services are profile-gated
   ([[docker-compose-build-noop]]). Or use `--build` on each `run`.
2. `docker compose --profile jobs run --rm jobs validate-config` — offline; expect
   `valid: chain=gnosis chain_id=100 tokens=3/3 pools=1/1 jobs=4`.
3. `docker compose --profile migrations run --rm migrations` — apply schema (first DB write).
   The entrypoint does **not** wait for ClickHouse; ensure it is reachable first.
4. `docker compose --profile jobs run --rm jobs probe --persist` — per-endpoint `ok` lines.
5. `docker compose --profile jobs run --rm jobs census --date <YYYY-MM-DD> --job daily_pool_reserves`
   — cheapest publishing job (pool/explicit jobs skip discovery).
6. `docker compose --profile jobs run --rm jobs validate` — must exit 0 (no conflicts).
7. `docker compose --profile daemon up -d daemon`, then `curl localhost:9090/{live,ready,health,metrics}`.

Env status (2026-07-21): all 22 vars in `.env` present with real values, matching
`.env.example` 1:1 — see [[settings-env-loading]]. `.env` targets live ClickHouse Cloud +
real RPC (contains secrets; gitignored + dockerignored). Full detail: `docs/runbook.md`.

Deployment shakeout (2026-07-21, first real deploy of this fresh repo): three env-side
blockers, then a real migration bug — all initially masked as `DatabaseError`:
1. `.env` password was quote-wrapped -> keep it unquoted ([[env-password-quoting]]).
2. Service user `state_indexer` lacked `CREATE DATABASE/VIEW`, `DROP VIEW` -> granted
   ([[clickhouse-cloud-migration-grants]]).
3. `007_views.sql` was invalid for ClickHouse 26.2's analyzer (aggregate/alias collisions and
   `SELECT p.*` name-prefixing) -> rewrote the views ([[clickhouse-analyzer-view-sql]]).
Migration `000`-`007` now applies and records cleanly (all 19 views validated on the server).
After editing a migration you must rebuild the image (`docker compose build`) so the container
copy matches the recorded checksum. Debugging technique: [[cli-hides-real-db-error]].
