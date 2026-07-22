---
name: cl-bitmap-convention-differs
symptom: Algebra/Swapr V3 tick discovery finds zero (or wrong) initialized ticks; ΣliquidityNet != 0
area: collectors
status: active
updated: 2026-07-22
---

## Symptom

CL tick discovery works for Uniswap V3 but an Algebra (Swapr V3) pool returns no initialized
ticks, or a different set than reality, so the `ΣliquidityNet == 0` / `Σnet(≤tick) == liquidity()`
invariants fail and nothing publishes. The naive assumption "both are Uniswap V3 forks, same bitmap
math" is wrong.

## Root cause

The two protocols index their tick bitmap by **different keys** (confirmed live on Gnosis
2026-07-22):

- **Uniswap V3 `tickBitmap(int16 wordPos)`** indexes the **compressed** tick:
  `compressed = tick / tickSpacing` (floor), `wordPos = compressed >> 8`, `bitPos = compressed & 0xff`.
- **Algebra V1 `tickTable(int16 wordPos)`** indexes the **raw** tick, *not* divided by tickSpacing:
  `wordPos = tick >> 8`, `bitPos = tick & 0xff`.

Verified: for Uniswap initialized tick 1570 (spacing 10) the bit is set under the compressed
convention (wordPos 0, bit 157) and clear under raw. For Algebra initialized tick −54360 (spacing 60)
the bit is set under the raw convention (wordPos −213, bit 168) and clear under compressed.

Consequence for scan range: Uniswap scans compressed wordPos over `[MIN_TICK/spacing >> 8,
MAX_TICK/spacing >> 8]`; Algebra must scan **raw** wordPos over `[MIN_TICK >> 8, MAX_TICK >> 8]` =
`[-3466, 3466]` (~6933 words/pool) to stay gap-free — much wider. Batch via Multicall3 and gate on
`min_active_liquidity`.

## Fix / correct pattern

Dispatch the bitmap decode by `pool_class`: a compressed-key path for `uniswap_v3`, a raw-key path
for `swapr_v3_algebra`. Reconstruct a set bit back to a tick as: Uniswap `tick = (wordPos*256 + bit)
* tickSpacing`; Algebra `tick = wordPos*256 + bit`. Then require the Σ invariants
([[cl-liquidity-profile]]) before publishing — they are the backstop that catches a wrong convention.

## How to avoid / detect

Never assume an "X V3 fork" shares X's bitmap/tick math — confirm the getter and its key convention
on a real pool first (raw eth_call, check a known-initialized tick's bit). The Σ invariants must be a
hard publish gate, not a warning, so a convention bug fails closed instead of publishing a truncated
profile. Signed `liquidityNet`/`liquidityDelta` is int128 — decode sign-extended ([[cl-liquidity-profile]]).
