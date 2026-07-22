---
name: catalog-incremental-refresh
type: project
updated: 2026-07-22
---

Pools are onboarded dynamically via a build-time incremental refresh (Phase 3, 2026-07),
not a one-shot enumeration. The catalog is still generated independently of dbt from on-chain
factory/Vault events ([[gnosis-dex-factories]]).

- **`scripts/catalog/enumerate.py`** — committed watermark `scripts/catalog/watermark.json`
  (`{"head": N}`, the last scanned head). `--incremental` scans each source from
  `watermark - overlap` (default overlap 10k blocks, a late-finality safety window) to head and
  decodes only new pool-creation events; no watermark → full history. The watermark advances
  only on a scan-to-head (no `--to-block`). Pure helper `scan_start(...)` is unit-tested.
  Captures Uniswap `fee` (PoolCreated topic3) + `tick_spacing` (data word 0), and Algebra
  `tick_spacing=60`, so the CL collector skips a per-anchor read ([[cl-liquidity-profile]]).
- **`scripts/catalog/assemble.py`** — host-run, **additive and non-destructive**: existing
  tokens/pools/jobs win on collision, new pools appended, nothing removed. Jobs are preserved
  (standard jobs incl. `daily_cl_liquidity` are seeded only when absent, so custom jobs survive).
  `curated_resolved.json` is optional (unchanged on a refresh). Path-parameterized
  (`assemble(cfg_dir, out_dir)`) for testing.
- **`make refresh-catalog`** — enumerate `--incremental` in the jobs container → assemble →
  `validate-config` on the host. Recommended: run in CI, open a PR with the `config/` diff, then
  rebuild the image (or dev-overlay live-mount `config/`).

**Why it's reindex-safe:** the effective-config hash is **per job-target**
(`census.register_configs` → `canonical_json(effective)` per pool/token), not a global digest.
A new pool is a new target with its own hash; existing targets' hashes and published history are
untouched. See [[config-change-triggers-reindex]].
