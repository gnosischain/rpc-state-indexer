---
name: settings-env-loading
type: ops
updated: 2026-07-21
---

`RuntimeSettings` (`src/rpc_state_indexer/settings.py`) is configured with
`env_file=None`, so **the application never reads `.env` itself** — it reads only the
real process environment (each field via its uppercase `alias`).

- **Docker:** `docker-compose.yml` injects it via `env_file: ${ENV_FILE:-.env}` on all
  three services.
- **Local CLI:** nothing auto-loads it — run `set -a; source .env; set +a` first, or the
  settings fall back to their per-field defaults.

There are 22 runtime vars; all are documented 1:1 in `.env.example`. As of 2026-07-21 the
working-tree `.env` has all 22 set with real values (see [[deploy-run-sequence]]). Test
suites read a separate set (`GNOSIS_ARCHIVE_RPC_URLS`, `CLICKHOUSE_TEST_*`) that the app
does not use.
