---
name: config-change-triggers-reindex
symptom: published rows disappeared after editing the catalog / a token/pool/job change
area: config
status: active
updated: 2026-07-21
---

## Symptom

After editing a token, pool, job, universe, chain, or vendored file, previously published
rows stop appearing in the `v_*_published` views, and `status`/`validate` show the date as
uncovered.

## Root cause

At startup the service registers an **effective-config hash** for each enabled job-target
(computed by `src/rpc_state_indexer/config/hashing.py`, which also hashes vendored CSVs).
Publication views only join publications whose hash equals the current one. A material
catalog edit changes the hash, so old publications no longer satisfy
`v_publications_eligible` — the data is not lost, it is just no longer "current". This is
intended: it forces a controlled reindex when the definition of a target changes.

## Fix / correct pattern

Treat a material catalog edit as a controlled reindex: `validate-config`, run one date,
compare universe size and call plan, then backfill/densify the affected history under the
new hash. Old raw attempts and publications remain as evidence.

## How to avoid / detect

Know that cosmetic vs material edits both change the hash if they touch hashed inputs.
Plan reindex scope before editing production catalog. See `config/AGENTS.md` and
`docs/runbook.md` §14. Related: [[clickhouse-published-contract]].
