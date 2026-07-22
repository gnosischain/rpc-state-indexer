# AGENTS.md — config/

The typed YAML **catalog**: it defines *what* to index. (Env vars define *how* the process
runs — see [`settings-env-loading`](../.agents/memory/settings-env-loading.md).) Loaded by
`../src/rpc_state_indexer/config/loader.py` into a `Catalog` and validated against the
pydantic models in `config/models.py`. Root rules: [`../AGENTS.md`](../AGENTS.md).

## Files

- `chains.yaml` — per-chain: `chain_id`, finality, the `multicall3` block (address,
  `deployment_block`, pinned `runtime_code_hash`), `legacy_execution` quorum rules, and
  `discovery` limits. The Multicall3 block is the execution boundary
  ([`multicall3-boundary`](../.agents/memory/multicall3-boundary.md)).
- `gnosis/tokens.yaml`, `gnosis/pools.yaml`, `gnosis/universes.yaml`, `gnosis/jobs.yaml` —
  the per-chain inventory (loaded by exact filename, so other files here are ignored).
- `gnosis/vendored/*.csv` — explicit address lists referenced by `explicit_list` universes.

## Rules when editing here

- The loader reads **named** files only (`chains.yaml`, `tokens.yaml`, `pools.yaml`,
  `universes.yaml`, `jobs.yaml`); adding docs like this file does not affect loading.
- Validate offline after any edit: `rpc-state-indexer validate-config` (expect
  `valid: chain=gnosis chain_id=100 tokens=3/3 pools=1/1 jobs=4` for the starter catalog).
  `load_catalog` also cross-checks pool/token/universe/job references and a duplicate-key
  guard rejects repeated YAML keys.
- Catalog and vendored CSVs feed the **effective-config hash** (`config/hashing.py`). A
  material edit changes the hash and drops old publications out of the current views —
  plan a controlled reindex:
  [`config-change-triggers-reindex`](../.agents/lessons/config-change-triggers-reindex.md).
- Verify every deployment block, lifetime, event set, pool asset, and aToken index source
  against chain data before treating the catalog as production
  (`docs/runbook.md` §17 checklist).
