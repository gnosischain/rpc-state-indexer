# Pre-Multicall3 history

## Short answer

History before Multicall3 is included.

The configured Gnosis Multicall3 contract was deployed at block `21,022,491`. The
indexer does not attempt to call it below that block. Instead, it automatically routes
the same historical state-call plan through verified direct JSON-RPC execution.

This means:

- WXDAI can be indexed from its configured deployment block `11,173,937`;
- WETH can be indexed from its configured deployment block `11,568,333`;
- any newly configured token can start at its own deployment block;
- the RPC provider still has to retain state for the requested block.

"Pre-Multicall" describes which batching mechanism is used. It does not mean that
those dates are excluded.

## Automatic routing

For every snapshot, the indexer first resolves a pinned day-end anchor. Then:

```python
if anchor.number < 21_022_491:
    use_verified_legacy_rpc_batch()
else:
    use_multicall3_aggregate3()
```

The collector is unaware of that choice. ERC-20, aToken, and pool collectors build
ordinary typed contract calls and receive the same verified-result interface from
either executor.

## Preferred legacy path: EIP-1898

When a provider supports EIP-1898, every pre-Multicall `eth_call` uses the canonical
block hash:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "eth_call",
  "params": [
    {"to": "0x...", "data": "0x..."},
    {"blockHash": "0x...", "requireCanonical": true}
  ]
}
```

The service tests EIP-1898 rather than trusting provider documentation. It makes a
successful call at a canonical historical block and verifies that Multicall3 reports
that exact block number. It then submits a bogus block hash, which must fail. An RPC
proxy that ignores the block object and serves `latest` cannot pass both checks.

The provider may accept HTTP JSON-RPC batches in any response order. The decoder maps
by request ID and rejects missing, duplicate, unknown, or malformed responses.

## Fallback path: independent provider quorum

If no usable endpoint supports EIP-1898, a block-number call is accepted only under a
stronger quorum procedure. The default Gnosis configuration requires two distinct
provider groups.

For each provider group:

1. read the hash of the pinned anchor number;
2. require it to equal the stored anchor hash;
3. execute every `eth_call` at that block number;
4. read and verify the anchor hash again;
5. compute a deterministic digest over all raw call outcomes.

The result is accepted only when the complete digest matches across all provider
groups. This catches disagreement about state, call ordering, success flags, return
bytes, and error results.

Do not label two endpoints from the same underlying RPC operator as independent
groups. That would preserve availability but defeat the failure-independence purpose
of the quorum.

## Batch sizing and provider limits

`LEGACY_RPC_BATCH_SIZE` controls the initial maximum body calls in a legacy batch.
When a provider returns a classified provider-limit error, the executor halves the
call set and retries recursively. A one-call provider-limit failure is terminal.

If an endpoint does not support HTTP JSON-RPC batches, the same call set is issued as
concurrent individual `eth_call`s under the same EIP-1898 or number/quorum proof.

Run a benchmark at a pinned historical date before a backfill:

```bash
rpc-state-indexer bench --date 2021-12-31
```

The benchmark records its result in `rpc_benchmarks`. Keep a conservative runtime
batch size after accounting for shared-provider load and traffic variability.

## Archive depth is the actual limit

The standard `probe` command separately verifies Multicall3 bytecode at block
`21,022,491` and performs `eth_getCode` plus a strictly decoded state call at the
earliest enabled token deployment. In the starter catalog that is WXDAI block
`11,173,937`. The successful block is stored as `archive_from_block` for the endpoint.
Adding an older enabled token moves this startup probe backward automatically.

Before a large pre-Multicall backfill:

1. confirm the oldest configured token and intended snapshot date;
2. resolve/benchmark that historical date on the actual paid endpoint tier;
3. run one narrow scoped census or the pinned-chain test at a representative block;
4. confirm the executor evidence reports `legacy_rpc_batch`;
5. only then start the broad discovery and backfill.

The opt-in pinned-chain test already exercises both sides of the boundary:

```bash
GNOSIS_ARCHIVE_RPC_URLS=https://rpc-a,https://rpc-b \
GNOSIS_ARCHIVE_PROVIDER_GROUPS=provider_a,provider_b \
pytest -m pinned_chain tests/pinned_chain/test_historical_execution.py
```

It reads WXDAI `totalSupply()` at:

- block `20,000,000` through `LegacyRpcBatchExecutor`;
- block `21,022,500` through `Multicall3Executor`.

## What is stored

Every verified batch records:

- `executor_kind` (`legacy_rpc_batch` or `multicall3`);
- `block_reference_kind` (`eip1898`, `number_quorum`, or the Multicall number/hash
  reference path);
- the anchor block and hash;
- the provider groups involved;
- body call count;
- raw result digest;
- verification flag.

The publication copies the combined executor/reference/provider evidence. A downstream
consumer can therefore audit how every published historical observation was obtained.
