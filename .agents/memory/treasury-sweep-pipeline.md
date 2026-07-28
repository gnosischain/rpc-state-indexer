---
name: treasury-sweep-pipeline
description: "GnosisDAO treasury tracking is discovery-driven: wallet sweep (logs) -> discovered token selector (runtime targets) -> scoped census; only the wallet CSV is curated"
metadata:
  type: project
---

The treasury pipeline (added 2026-07) inverts the usual catalog flow. The only curated
input is `config/<chain>/vendored/treasury_addresses.csv` (23 GnosisDAO wallets from
github.com/koeppelmann/GnosisDAO_treasury; `0x604e…350c` is the optionally-excluded
Gnosis Ltd. wallet — keep it in the CSV, exclusion is a downstream `WHERE holder`).

- **Sweep** (`core/sweep_service.py`, `config/<chain>/sweeps.yaml`, migration 011):
  address-less `eth_getLogs` with the wallets OR-listed at one indexed topic position at a
  time (positions 1..3). Coverage per (wallet, position) in `sweep_ranges` — adding a
  wallet backfills only that wallet. Raw hits in `wallet_interaction_logs`; candidates
  via `v_sweep_candidate_tokens` (topic-shape classification: same Transfer topic0 is
  erc20 at 3 topics, erc721 at 4) and `v_sweep_candidate_protocols` (everything else —
  the queue for keyed-call adapters: Morpho `onBehalf`, Balancer InternalBalanceChanged).
- **Measurement**: `token_selector: {discovered: true}` (scoped-only, enforced) resolves
  targets at census time in `IndexerService._token_targets`: curated catalog entry wins
  when the address overlaps (disabled curated entry = kill-switch), otherwise
  `discovered_token_config()` synthesizes a standard_erc20 with symbol=address and
  deployment_block=first_seen_block (auto coverage-from-first-interaction). Targets are
  registered in config_registry at resolution — publications are invisible without that
  registration (v_publications_eligible inner-joins it).
- **Quarantine** is derived, not stored: last N attempts all 'failed' (window over
  census_attempts; `DISCOVERED_QUARANTINE_THRESHOLD`) excludes a target from new batches,
  logged via `discovered_targets_quarantined`. Spam dies downstream (no price → no NAV),
  never by list-gatekeeping.
- **Chains**: `ethereum` chain in chains.yaml (Multicall3 block 14353601, same runtime
  code hash as gnosis — deterministic deploy, verified against two providers; GNO seed
  token exists only because the archive probe needs one enabled token). Same DB, one
  writer per chain.

Not yet built (plan phases 4–5): native ETH/xDAI via `getEthBalance`, NFPM position
enumeration, config-driven keyed-call adapters for non-tokenized protocols, trace census,
beacon withdrawal-credential scan. Related: [[gnosis-catalog-scale]],
[[clickhouse-published-contract]].
