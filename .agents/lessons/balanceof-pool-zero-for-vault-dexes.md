---
name: balanceof-pool-zero-for-vault-dexes
symptom: a DEX pool's reserves publish as 0 (or near-0) though the pool clearly has liquidity
area: collectors
status: mitigated
updated: 2026-07-21
---

## Symptom

Pool reserves for a Balancer (or any Vault-custody AMM) pool come out as ~0, while the pool
holds real liquidity. No error is raised — the numbers are just wrong.

## Root cause

`PoolReserveCollector` computes reserves as `token.balanceOf(pool_address)`
(`collectors/pools.py`). That is correct only when the pool contract custodies its own tokens
(Uniswap V3, Swapr V3/Algebra). Balancer V2/V3 hold all pool tokens in a singleton **Vault**,
so `balanceOf(pool)` is ~0 — a silently-wrong reserve, not a loud failure. The collector does
not branch on `pool_class`, so it would apply the wrong model to any Vault-custody pool.

## Fix / correct pattern

Read Vault-custody pools from the Vault, dispatched by `pool_class` — see
[[balancer-vault-custody]] (`BalancerPoolCollector`, `core/census.py` `run_pool`). Never add a
Vault-custody DEX as a plain `balanceOf` pool. When onboarding a new DEX, first check where the
tokens actually sit (pool contract vs a shared vault/singleton) before choosing the collector.

## How to avoid / detect

Spot-check a new pool's published reserves against an explorer/subgraph on the first run. A
reserve that is orders of magnitude too small is the tell. The Balancer collector cross-checks
returned tokens against configured assets to catch stale/wrong pool config.
