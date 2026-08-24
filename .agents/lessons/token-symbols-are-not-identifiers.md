---
name: token-symbols-are-not-identifiers
symptom: "a treasury/portfolio total is absurd (trillions of USDC); grouping or pricing by token symbol"
area: consumers
status: active
---

## Symptom

Aggregating discovered treasury balances by `symbol` on mainnet returned **8.19 trillion USDC**
against a real holding of roughly 600k, and 417 wallet rows where only 23 wallets exist.

## Root cause + fix

**42 distinct contracts on Ethereum call themselves `USDC`** in the GnosisDAO treasury's discovered
set alone. The impersonators set `decimals = 6` to match, and mint ~1e12 units to every treasury
wallet, so they are indistinguishable from the real token by metadata alone:

```
0x357eb8dc76920a7a00d8e3059cdb0249aceb2df7  dp=6  1,077,801,273,269.25
0x20e54e495a1074efbdd6acf51f468986730aa208  dp=6    999,516,884,888.11
...                                                (42 contracts total)
0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48  dp=6  <- the only real one
```

Symbols are attacker-controlled free text, not identifiers. Any `GROUP BY symbol`, any join to a
price feed keyed on `(symbol, date)`, and any allowlist expressed as symbols is exploitable — the
attacker simply picks the symbol you trust.

Fix: key everything on `(chain_id, token_address)`. Symbol and name are **display-only**. This is
why the price integration must use an address-keyed source (CoinGecko's
`simple/token_price/{platform}?contract_addresses=...` and
`coins/{platform}/contract/{address}/market_chart`) rather than a symbol-keyed model: a fake USDC
has a different address, so it simply gets no price, and no allowlist is needed.

## How to avoid / detect

Detection query — if this returns anything, some consumer is at risk:

```sql
SELECT symbol, uniqExact(token_address) AS contracts
FROM rpc_state_indexer.v_treasury_balances
WHERE chain_id = 1 GROUP BY symbol HAVING contracts > 1 ORDER BY contracts DESC;
```

Related: a much better spam signal than symbol or transfer shape is **how many of the tracked
wallets hold the token**. Airdrop spam is blasted to *all* of them uniformly
(`uniqExact(wallet_address) = 23`), while genuine holdings sit in 1-4 wallets. Unlike
[[airdrop-shape-is-not-a-spam-filter]], this one is checkable against the balance data itself.
See [[treasury-sweep-pipeline]].
