# AGENTS.md — execution/

Verified historical state execution. This package turns a set of `(subject, call)` reads
at a pinned block into verified results, choosing the regime by block number. Root rules:
[`../../../AGENTS.md`](../../../AGENTS.md). Background facts:
[`multicall3-boundary`](../../../.agents/memory/multicall3-boundary.md),
[`provider-group-quorum`](../../../.agents/memory/provider-group-quorum.md).

## The two-regime boundary

`router.py` (`HistoricalExecutorRouter`) selects purely by block vs the Multicall3
deployment block in `config/chains.yaml` (Gnosis: 21,022,491):

- **At/after** → `multicall3.py`: Multicall3 `aggregate3`. Every batch carries
  block/timestamp/parent-hash **sentinels at head and tail**; a batch that cannot prove
  the pinned anchor raises `BatchVerificationError` (never publishes).
- **Before** → `legacy_rpc_batch.py`: verified `eth_call` batching. Prefer EIP-1898
  hash-pinning; otherwise a number-pinned **hash sandwich** requiring matching results
  from a quorum of distinct provider groups (quorum 2 on Gnosis). Disagreement →
  `ProviderQuorumMismatch`, no publish.

`code.py` (`HistoricalCodeVerifier`) proves contract bytecode at the pinned block before
trusting a read. `batch_planner.py` sizes/splits batches; classified provider-limit
failures are adaptively split, but a genuine failure still fails closed.

## Rules when editing here

- Never widen a failure into a success or a zero — surface it as an error. See
  [`never-coerce-failure-to-zero`](../../../.agents/lessons/never-coerce-failure-to-zero.md).
- Keep sentinel checks at both head and tail; do not "optimize" one away.
- Do not promote unlabeled providers into an independence proof (they are `unclassified`).
- Add offline tests for every new failure branch; keep the executor deterministic and free
  of import-time network calls.
