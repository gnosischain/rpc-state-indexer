---
name: multicall3-boundary
type: invariant
updated: 2026-07-21
---

Gnosis Multicall3 (`0xca11bde05977b3631167028862be2a173976ca11`) deploys at block
**21,022,491** (`config/chains.yaml`). This block is the execution-regime boundary:

- **At/after** it: `HistoricalExecutorRouter` uses Multicall3 `aggregate3` with
  block/timestamp/parent-hash sentinels at head and tail of every batch.
- **Before** it: the router uses the legacy verified `eth_call` batch path (EIP-1898
  hash-pinning, or a number-pinned "hash sandwich" quorum across distinct provider groups).

Regime selection is purely by block number in
`src/rpc_state_indexer/execution/router.py`; the pinned `runtime_code_hash` in
`config/chains.yaml` is verified at the deployment block before trusting the contract.
Legacy verification requires independent providers — see [[provider-group-quorum]].
