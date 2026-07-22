---
name: balancer-vault-custody
type: invariant
updated: 2026-07-21
---

Balancer custodies pool tokens in a chain-singleton **Vault**, so a Balancer pool's own
`balanceOf` is ~0 — reserves must be read from the Vault, one call per pool:

- **V2** `getPoolTokens(bytes32 poolId)` -> `(address[] tokens, uint256[] balances, uint256)`.
  Pools keyed by `poolId`. Gnosis Vault `0xba12222222228d8ba445958a75a0704d566bf2c8`.
- **V3** `getPoolTokenInfo(address pool)` -> `(address[] tokens, TokenInfo[], uint256[]
  balancesRaw, uint256[] scaled18)`; take **`balancesRaw`** (exact integers). Pools keyed by
  address. Gnosis Vault `0xba1333333333a1ba1108e8412f11850a5c319ba9`.

Implemented in `collectors/balancer.py` (`BalancerPoolCollector`), dispatched by `pool_class`
in `core/census.py` `run_pool`; Vaults live in `chains.yaml` `balancer.{v2_vault,v3_vault}`;
`PoolConfig.pool_id` is required for `balancer_v2`. Decoders + calldata in `evm/decoding.py`
(`decode_balancer_v2_pool_tokens` / `decode_balancer_v3_pool_token_info`) and
`evm/calldata.py` (selectors `f94d4668` / `67e0e076`), verified against live Gnosis Vaults.
Composable pools list their own BPT in the return — configured assets must be a **subset** of
the Vault's tokens; extras are ignored, a missing configured asset fails. Uniswap V3 and Swapr
V3 (Algebra) custody their own reserves, so they use the plain `balanceOf(pool)` collector.
Related trap: [[balanceof-pool-zero-for-vault-dexes]].
