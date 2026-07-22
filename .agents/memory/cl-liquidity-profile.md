---
name: cl-liquidity-profile
type: reference
updated: 2026-07-22
---

Concentrated-liquidity (Uniswap V3 + Swapr/Algebra) tick primitives to ingest, and the
**on-chain struct layouts confirmed live on Gnosis** (2026-07-22) against
`0x01343cf42c7f1f71b230126dda3b7b2c108e9f2e` (liquid Uniswap V3, spacing 10) and
`0xa3c906c657454d77ce684355adb3067d6749bdc2` (Swapr V3 / Algebra, spacing 60).

**Pool state (1 row/pool/anchor):**
- Uniswap `slot0()` → 7 words: `[0]` sqrtPriceX96 (uint160), `[1]` **tick int24 (signed)**,
  `[2]` observationIndex, `[3..4]` cardinality, `[5]` feeProtocol, `[6]` unlocked.
  Plus `liquidity()`→uint128, `feeGrowthGlobal0X128()`/`feeGrowthGlobal1X128()`→uint256,
  immutables `tickSpacing()`→int24, `fee()`→uint24.
- Algebra `globalState()` → 7 words: `[0]` price=sqrtPriceX96 (uint160), `[1]` **tick int24
  (signed)**, `[2]` fee (uint16, dynamic), `[3]` timepointIndex, `[4..5]` communityFee0/1,
  `[6]` unlocked. Plus `liquidity()`→uint128, `totalFeeGrowth0Token()`/`totalFeeGrowth1Token()`
  →uint256, `tickSpacing()`→int24 (=60 here). Fee is dynamic (read from globalState, not `fee()`).

**Per-initialized-tick — `ticks(int24)` → 8 words, SAME shape both protocols:**
`[0]` liquidityGross/Total (uint128), `[1]` **liquidityNet/Delta — int128 SIGNED**
(live-confirmed negatives: −64967712697441336847353125 Uniswap, −46947697606097002059 Algebra),
`[2]` feeGrowthOutside0/outerFeeGrowth0 (uint256), `[3]` feeGrowthOutside1 (uint256),
`[4]` tickCumulativeOutside (int56), `[5]` secondsPerLiquidityOutside (uint160),
`[6]` secondsOutside (uint32), `[7]` initialized (bool).

**Tick discovery bitmaps differ by protocol — see [[cl-bitmap-convention-differs]]:**
Uniswap `tickBitmap(int16)` indexes the **compressed** tick (`tick/tickSpacing`);
Algebra `tickTable(int16)` indexes the **raw** tick. Both uint256 words.

**Self-verifying invariants (publish only if they hold):** Σ`liquidityNet` over all initialized
ticks **== 0**; Σ`liquidityNet` for ticks ≤ active tick **== `liquidity()`**. These self-validate
the signed decode — a botched sign flips the sums. int24 tick range is [−887272, 887272] (TickMath).

Scale: an active pool has tens–hundreds of initialized ticks. Gate ingestion on
`liquidity() >= min_active_liquidity` (state-only below that). See [[gnosis-catalog-scale]],
[[indexer-two-layer-architecture]]. Reconstruction walk lives in the compute layer, not ingestion.
