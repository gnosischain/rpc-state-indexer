---
name: writer-heartbeat-refusal
symptom: startup refuses — another writer is active / stale writer
area: ops
status: active
updated: 2026-07-21
---

## Symptom

A census/backfill/densify/daemon process exits at startup complaining that another writer
holds the chain, even though you believe nothing else is running.

## Root cause

Write operations acquire a chain-scoped heartbeat in `writer_heartbeats`. Startup refuses
when another heartbeat is newer than `WRITER_STALE_SECONDS` (default 120). This fires when
(a) two writers really overlap, or (b) a previous process died uncleanly without writing
its released state, so its last heartbeat is still within the stale window.

## Fix / correct pattern

Run only one writer per chain. After an unclean death, **wait out** `WRITER_STALE_SECONDS`
and confirm no writer is actually alive before retrying — there is deliberately no
force-overlap flag. Clean shutdowns release immediately.

## How to avoid / detect

Sequence write steps (don't launch overlapping `docker compose run` jobs); the deploy
sequence in [[deploy-run-sequence]] runs them one at a time. Background: the guard is
[[single-writer-heartbeat]].
