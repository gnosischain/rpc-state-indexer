---
name: docker-compose-build-noop
symptom: "docker compose build never does anything" / image keeps using stale files
area: ops
status: active
updated: 2026-07-21
---

## Symptom

`docker compose build` produces no output and does not rebuild; the container keeps running
old code/migrations even after you edit files. A migration edit then fails with
`applied migration changed: NNN_*.sql` (checksum mismatch), because the ledger has the new
checksum but the image still holds the old file.

## Root cause

Every service in `docker-compose.yml` is behind a `profiles:` gate (`daemon`, `migrations`,
`jobs`). A bare `docker compose build` (no service name, no `--profile`) matches **no**
service and silently builds nothing. The image `rpc-state-indexer:local` is created lazily
the first time a `run` auto-builds a missing image, and is never rebuilt again unless you ask
for it — so edits never reach the container.

## Fix / correct pattern

Name the service (bypasses profile gating; all three services share the `rpc-state-indexer:local`
tag, so building one updates the image):

```bash
docker compose build migrations          # or: docker compose --profile migrations build
```

Or force a rebuild at run time (this is what `make run-migrations` does):

```bash
docker compose --profile migrations run --rm --build migrations
```

Verify the image actually updated:
`docker compose --profile jobs run --rm --entrypoint sh jobs -c 'sha256sum migrations/007_views.sql'`.

## How to avoid / detect

Prefer the `make` targets (`make run-migrations`, `make job ARGS=...`) — they already include
`--build`. If a file edit "isn't taking", check the file's checksum inside the image before
assuming the code is wrong. Related ledger behavior: [[migrations-are-immutable]].
