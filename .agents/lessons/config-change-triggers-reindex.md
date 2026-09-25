---
name: config-change-triggers-reindex
symptom: published rows disappeared after editing the catalog / a token/pool/job change
area: config
status: active
updated: 2026-09-25
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

## Incident 2026-09-08: a code change, not a catalog edit

fa80947 added `universe_aliases: list[Address] = []` to `TokenConfig` for the two
tokens that share a ledger (EURe v1/v2, later GBPe). The effective config hashes the full
`model_dump()` of the target, so EVERY token on both chains hashed `"universe_aliases": []`
and got a new config hash on the next daemon start (8 Sept 12:36 UTC on Ethereum, 9 Sept
07:06 UTC on Gnosis). ~3.4M publications (2020 → 2026-09-07) dropped out of every
`v_*_published` view — the Governance Explorer treasury tab showed one month of history —
while the raw data, the backfill skip gate and dbt (which ignores the hash) all looked
fine. Proven by recomputing: fa80947~1 reproduces all 3,478 catalog token hashes on the
pre-change publications; the only effective-config diff is `target/universe_aliases = []`.

Fix: `HASH_NEUTRAL_WHEN_EMPTY_TARGET_FIELDS` in `config/loader.py` drops the field while
empty (the same rule `discovered: false` already had), restoring the pre-8-Sept hashes
exactly. Rows published since then under the interim hash need a re-census with
`SKIP_PUBLISHED_ANY_CONFIG_HASH=false`. `tests/unit/test_config_hashing.py` now pins
production hashes and fails when a new target field is not listed.

## How to avoid / detect

Know that cosmetic vs material edits both change the hash if they touch hashed inputs.
A new defaulted MODEL field is a hashed input too: list it as hash-neutral when empty.
Plan reindex scope before editing production catalog. See `config/AGENTS.md` and
`docs/runbook.md` §14. Related: [[clickhouse-published-contract]].
