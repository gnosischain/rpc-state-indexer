---
name: provider-group-quorum
type: invariant
updated: 2026-07-21
---

`RPC_PROVIDER_GROUPS` is a comma-separated label per `RPC_URLS` entry (same length, or
the settings loader raises). Groups must be **genuinely independent** infrastructure —
they are the basis of the pre-Multicall independence proof.

For the legacy number-fallback path (`config/chains.yaml` →
`legacy_execution.number_fallback`): `required_provider_quorum: 2`,
`require_distinct_provider_groups: true`, `hash_sandwich: true`. So a pre-Multicall read
that can't use EIP-1898 needs matching results from **2 distinct provider groups**.

If `RPC_PROVIDER_GROUPS` is empty, all endpoints collapse to `unclassified`
(`settings.py`), which can never form a quorum — EIP-1898-capable endpoints still work,
but the numeric fallback for pre-Multicall history will not. Current `.env` sets
`tenderly,internal` (2 groups). Related: [[multicall3-boundary]].
