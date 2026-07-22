---
name: gnosis-dex-factories
type: reference
updated: 2026-07-21
---

On-chain-confirmed Gnosis DEX factory/vault addresses + pool-creation event topics used by the
catalog enumerator (`scripts/catalog/`). Confirmed via RPC 2026-07-21.

- **Uniswap V3** factory `0xe32f7dd7e3f098d518ff19a22d5f028e076489b1` (NOT the canonical
  `0x1f98431c…`, which has no code on Gnosis; resolved via NonfungiblePositionManager
  `0xae8fbe656a77519a7490054274910129c9244fa3` `.factory()`). Event
  `PoolCreated(address indexed token0, address indexed token1, uint24 indexed fee, int24, address)`,
  topic0 `0x783cca1c…`; pool address is the 2nd data word. Low activity on Gnosis.
- **Swapr V3 (Algebra)** factory `0xa0864cca6e114013ab0e27cbd5b6f4c8947da766`. Event
  `Pool(address indexed token0, address indexed token1, address pool)`, topic0 `0x91ccaa7a…`.
- **Balancer V2** vault `0xba12222222228d8ba445958a75a0704d566bf2c8`. `PoolRegistered(bytes32
  indexed poolId, address indexed pool, uint8)` topic0 `0x3c13bc30…` + `TokensRegistered(bytes32
  indexed poolId, address[], address[])` (join by poolId for tokens). Reserves: [[balancer-vault-custody]].
- **Balancer V3** vault `0xba1333333333a1ba1108e8412f11850a5c319ba9`. Very high log volume;
  enumerate pool addresses from its `PoolRegistered` (pool = indexed topic1), then read tokens via
  `getPoolTokens(address)`.

Gnosis RPC log limits (rpc_2 internal node): `eth_getLogs` max **100k block range** AND max
**20k results** — chunk and split on both. Filtering by the pool-creation topic0 keeps result
counts low, so 100k chunks are fine for enumeration.
