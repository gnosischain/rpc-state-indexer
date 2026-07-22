"""Assemble the enumerated + curated catalog into config/gnosis/*.yaml.

Host-run (no RPC). **Additive and non-destructive**: existing tokens/pools/jobs win on
collision, new enumerated entries are appended, nothing is removed. This makes it safe to run
after an incremental enumeration (see enumerate.py --incremental) — a new pool is a new target
with its own effective-config hash; existing targets' hashes and published history are
untouched (per-target hashing in census.register_configs). See
[[catalog-incremental-refresh]].

Merges:
  - existing hand-curated tokens/pools (kept on address collision — preserves weth9_fork /
    aToken classes and any manual edits),
  - the enumerated bulk (out/{tokens,pools}.json),
  - the curated full_supply set (out/curated_resolved.json) **if present** (optional for an
    incremental refresh, where the curated set is unchanged).

Jobs are preserved: every existing job is kept; the standard jobs (incl. daily_cl_liquidity)
are seeded only when absent, so custom/hand-tuned jobs survive a refresh untouched.
"""

from __future__ import annotations

import argparse
import json
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CFG = os.path.join(ROOT, "config", "gnosis")
DEFAULT_OUT = os.path.join(ROOT, "scripts", "catalog", "out")
TRANSFER = [{"abi": "erc20", "event": "Transfer", "holder_topics": [1, 2]}]
SUPPLY_PROBE = "0x000000000000000000000000000000000000dead"  # throwaway sentinel holder


def _load_yaml(cfg_dir: str, name: str) -> dict:
    path = os.path.join(cfg_dir, name)
    if not os.path.exists(path):
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def _load_json(out_dir: str, name: str, default):
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        return default
    with open(path) as fh:
        return json.load(fh)


def standard_jobs(curated_addrs: list[str]) -> dict:
    """The jobs assemble knows how to generate. Selectors are dynamic (all_enabled/class_in),
    so newly enumerated pools/tokens are picked up without regenerating these definitions."""
    jobs = {
        "daily_pool_reserves": {
            "target_kind": "pools", "pool_selector": {"all_enabled": True},
            "cadence": "daily", "integrity_mode": "pool_assets", "coverage_start": None,
        },
        "daily_token_supply": {
            "target_kind": "tokens",
            "token_selector": {"class_in": ["standard_erc20", "weth9_fork"]},
            "universe": "supply_probe", "cadence": "daily",
            "integrity_mode": "scoped", "coverage_start": None,
        },
        "daily_atokens_full": {
            "target_kind": "tokens",
            "token_selector": {"class_in": ["aave_v3_atoken", "spark_atoken"]},
            "universe": "full_holders", "cadence": "daily",
            "integrity_mode": "scaled_full_supply", "coverage_start": None,
        },
        "daily_treasury": {
            "target_kind": "tokens", "token_selector": {"all_enabled": True},
            "universe": "treasury", "cadence": "daily",
            "integrity_mode": "scoped", "coverage_start": None,
        },
        "daily_cl_liquidity": {
            "target_kind": "pools",
            "pool_selector": {"class_in": ["uniswap_v3", "swapr_v3_algebra"]},
            "cadence": "daily", "integrity_mode": "cl_liquidity", "coverage_start": None,
        },
    }
    if curated_addrs:  # only regenerate the curated-balances job when the curated set is known
        jobs["daily_curated_balances"] = {
            "target_kind": "tokens", "token_selector": {"addresses": sorted(curated_addrs)},
            "universe": "full_holders", "cadence": "daily",
            "integrity_mode": "full_supply", "coverage_start": None,
        }
    return jobs


def assemble(cfg_dir: str, out_dir: str) -> dict:
    existing_tokens = {
        t["address"].lower(): t for t in _load_yaml(cfg_dir, "tokens.yaml").get("tokens", [])
    }
    existing_pools = {
        p["address"].lower(): p for p in _load_yaml(cfg_dir, "pools.yaml").get("pools", [])
    }
    enum_tokens = _load_json(out_dir, "tokens.json", {"tokens": []})["tokens"]
    enum_pools = _load_json(out_dir, "pools.json", {"pools": []})["pools"]
    curated = _load_json(out_dir, "curated_resolved.json", {})

    # --- tokens: existing wins; then enumerated; then curated (lower deploy block only) ---
    tokens = dict(existing_tokens)
    for t in enum_tokens:
        tokens.setdefault(t["address"].lower(), t)
    for raw_addr, meta in curated.items():
        addr = raw_addr.lower()
        if addr in tokens:
            tokens[addr]["deployment_block"] = min(
                tokens[addr]["deployment_block"], meta["deployment_block"]
            )
        else:
            tokens[addr] = {
                "address": addr, "symbol": meta["symbol"] or "?",
                "decimals": meta["decimals"], "token_class": "standard_erc20",
                "deployment_block": meta["deployment_block"], "discovery_events": TRANSFER,
            }

    # --- pools: existing wins; then enumerated (additive) ---
    pools = dict(existing_pools)
    for p in enum_pools:
        pools.setdefault(p["address"].lower(), p)

    curated_addrs = [
        addr for addr in (a.lower() for a in curated)
        if addr in tokens and tokens[addr]["token_class"] in ("standard_erc20", "weth9_fork")
    ]

    # --- jobs: preserve every existing job; seed standard jobs only when absent ---
    jobs = dict(_load_yaml(cfg_dir, "jobs.yaml").get("jobs", {}))
    for name, spec in standard_jobs(curated_addrs).items():
        jobs.setdefault(name, spec)

    universes = _load_yaml(cfg_dir, "universes.yaml").get("universes", {})
    universes.setdefault(
        "supply_probe",
        {"kind": "explicit_list", "source": "vendored/supply_probe.csv",
         "address_column": "address"},
    )

    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "tokens.yaml"), "w") as fh:
        yaml.safe_dump({"tokens": list(tokens.values())}, fh, sort_keys=False)
    with open(os.path.join(cfg_dir, "pools.yaml"), "w") as fh:
        yaml.safe_dump({"pools": list(pools.values())}, fh, sort_keys=False)
    with open(os.path.join(cfg_dir, "universes.yaml"), "w") as fh:
        yaml.safe_dump({"universes": universes}, fh, sort_keys=False)
    with open(os.path.join(cfg_dir, "jobs.yaml"), "w") as fh:
        yaml.safe_dump({"jobs": jobs}, fh, sort_keys=False)

    vendored = os.path.join(cfg_dir, "vendored")
    os.makedirs(vendored, exist_ok=True)
    supply_csv = os.path.join(vendored, "supply_probe.csv")
    if not os.path.exists(supply_csv):
        with open(supply_csv, "w") as fh:
            fh.write(f"address\n{SUPPLY_PROBE}\n")

    return {"tokens": len(tokens), "pools": len(pools), "jobs": len(jobs),
            "curated_balances": len(curated_addrs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-dir", default=DEFAULT_CFG)
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    args = ap.parse_args()
    summary = assemble(args.config_dir, args.out_dir)
    print("  ".join(f"{k}: {v}" for k, v in summary.items()))


if __name__ == "__main__":
    main()
