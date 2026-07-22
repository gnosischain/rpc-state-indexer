---
name: single-writer-heartbeat
type: invariant
updated: 2026-07-21
---

Every write operation (census/backfill/densify/daemon) acquires a **chain-scoped
heartbeat** in the `writer_heartbeats` table via `WriterGuard`
(`src/rpc_state_indexer/service.py`). Startup **refuses** if another heartbeat for the
same chain is newer than `WRITER_STALE_SECONDS` (default 120, min 30). The live writer
refreshes at roughly one third of that window and writes a released state on clean
shutdown.

There is no force-overlap flag. Run only one writer per chain at a time; run write steps
sequentially, not concurrently. See the failure mode in [[writer-heartbeat-refusal]].
