---
name: airdrop-shape-is-not-a-spam-filter
symptom: "wanting to cut discovered-token census cost by dropping 'airdrop-shaped' tokens (observations <= wallets)"
area: census
status: active
---

## Symptom

The wallet sweep discovers ~950 ERC20 candidates on mainnet, most of them obvious spam. The
tempting optimization is to skip the ~500 whose sweep shape is `observations <= wallets` — a single
transfer to each wallet, the classic airdrop-blast signature — and census only the "recurring"
ones. It looks like a structural filter rather than identity curation, so it feels principled.

## Root cause + fix

Measured on the first ethereum treasury census (2026-07-27), over the first 114 published tokens:

| shape | tokens | with a non-zero balance |
|---|---|---|
| one-shot (`observations <= wallets`) | 68 | **24** |
| recurring | 48 | 8 |

The filter would have discarded **three quarters of the tokens that actually hold value**.

`observations <= wallets` does not mean "spam" — it means "received once and never moved again",
which describes worthless airdrops *and* legitimate one-time distributions the treasury still
holds. Meanwhile actively-traded ("recurring") tokens are the ones most likely to have been sold
down to zero. The heuristic is inversely correlated with what it appears to select.

Fix: don't filter. Spam is retired by the mechanisms already in place — unpriced tokens never enter
NAV downstream, and targets that repeatedly fail to read are quarantined. Cost is separately
bounded by `deployment_block = first_seen_block` on discovered targets: the `_active` gate skips
any target whose first interaction post-dates the anchor, so historical dates measure only the
tokens that existed in the treasury *then*, not today's full set.

## How to avoid / detect

Before adopting any "cheap structural signal" for dropping targets, measure it against the outcome
it is supposed to predict (here: does the token hold a non-zero balance?). A filter that sounds
structural can still encode a wrong assumption. Related: [[treasury-sweep-pipeline]],
[[never-coerce-failure-to-zero]] (the same instinct — never let convenience remove observations).
