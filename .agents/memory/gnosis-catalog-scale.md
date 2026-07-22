---
name: gnosis-catalog-scale
type: project
updated: 2026-07-21
---

The Gnosis catalog was expanded from the 3-token/1-pool starter to full DEX coverage on
2026-07-21, generated on-chain by `scripts/catalog/enumerate.py` (+ `assemble.py`). Counts:
**3,422 tokens, 4,022 pools** (Uniswap V3 142, Swapr V3 2,379, Balancer V2 1,302, V3 219).

Jobs (`config/gnosis/jobs.yaml`):
- `daily_pool_reserves` — all pools; per-pool `pool_class` dispatch (balanceOf for Uni/Swapr,
  Vault for Balancer). ~4k census attempts/day.
- `daily_token_supply` — `totalSupply` for all standard/weth9 tokens via a `scoped` job over the
  `supply_probe` universe (one sentinel holder `0x…dead`; read supply from
  `v_token_scalars_published`, ignore the sentinel balance). ~3.4k attempts/day.
- `daily_curated_balances` — full holder balances (`full_supply`) for **58 curated** standard
  tokens (Blockscout top-50 by mcap ∪ dbt `tokens_whitelist`, aTokens/native excluded);
  triggers `full_holders` discovery from each token's real deploy block.
- `daily_atokens_full` (scaled) + `daily_treasury` (scoped) unchanged.

**Scale caveat:** all-pools + all-tokens = ~7.4k single-writer census attempts/day — a full
daily run is long. Many Swapr/Balancer pools are drained (genuine 0 reserves — a valid
observation, not a failure). To cut volume, filter to pools/tokens with liquidity, or shard by
`--job`. aTokens from the whitelist (17) still need a `scaled_full_supply` job with per-token
`index_source` — deferred. Enumeration intermediates live in gitignored `scripts/catalog/out/`.
Related: [[gnosis-dex-factories]], [[balancer-vault-custody]].
