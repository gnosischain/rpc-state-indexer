"""Incremental catalog refresh: watermark math + the additive/non-destructive assemble.

The critical property: a refresh only APPENDS new pools/tokens; it never rewrites an existing
target or drops a job (notably daily_cl_liquidity). A new pool is a new census target with its
own effective-config hash, so existing targets' hashes and published history are untouched.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import yaml

ROOT = Path(__file__).parents[2]


def _load(module_name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


enumerate_mod = _load("catalog_enumerate", ROOT / "scripts/catalog/enumerate.py")
assemble_mod = _load("catalog_assemble", ROOT / "scripts/catalog/assemble.py")


# --- watermark / scan_start --------------------------------------------------------
def test_scan_start_full_history_when_no_watermark() -> None:
    assert enumerate_mod.scan_start(
        explicit_from=None, incremental=True, watermark=None, overlap=10_000
    ) is None


def test_scan_start_not_incremental_is_full_history() -> None:
    assert enumerate_mod.scan_start(
        explicit_from=None, incremental=False, watermark=5_000_000, overlap=10_000
    ) is None


def test_scan_start_incremental_rescans_overlap_window() -> None:
    assert enumerate_mod.scan_start(
        explicit_from=None, incremental=True, watermark=5_000_000, overlap=10_000
    ) == 4_990_000


def test_scan_start_incremental_floors_at_zero() -> None:
    assert enumerate_mod.scan_start(
        explicit_from=None, incremental=True, watermark=5_000, overlap=10_000
    ) == 0


def test_scan_start_explicit_from_wins() -> None:
    assert enumerate_mod.scan_start(
        explicit_from=42, incremental=True, watermark=5_000_000, overlap=10_000
    ) == 42


def test_watermark_roundtrip(tmp_path: Path) -> None:
    path = str(tmp_path / "watermark.json")
    assert enumerate_mod.load_watermark(path) is None
    enumerate_mod.save_watermark(path, 12_345)
    assert enumerate_mod.load_watermark(path) == 12_345


# --- additive assemble -------------------------------------------------------------
EXISTING_POOL = {
    "address": "0x" + "b2" * 20, "name": "existing v2", "pool_class": "balancer_v2",
    "deployment_block": 100, "pool_id": "0x" + "cd" * 32,
    "assets": [{"token": "0x" + "11" * 20}],
}
NEW_POOL = {
    "address": "0x" + "c3" * 20, "name": "uniswap_v3", "pool_class": "uniswap_v3",
    "deployment_block": 5_000_000, "tick_spacing": 10, "fee": 500,
    "assets": [{"token": "0x" + "22" * 20}],
}
CL_JOB = {
    "target_kind": "pools",
    "pool_selector": {"class_in": ["uniswap_v3", "swapr_v3_algebra"]},
    "cadence": "daily", "integrity_mode": "cl_liquidity", "coverage_start": None,
}
CUSTOM_JOB = {
    "target_kind": "pools", "pool_selector": {"addresses": [EXISTING_POOL["address"]]},
    "cadence": "manual", "integrity_mode": "pool_assets", "coverage_start": None,
}


def _seed_catalog(cfg: Path, out: Path) -> None:
    cfg.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    (cfg / "tokens.yaml").write_text(yaml.safe_dump({"tokens": [
        {"address": "0x" + "11" * 20, "symbol": "AAA", "decimals": 18,
         "token_class": "standard_erc20", "deployment_block": 50,
         "discovery_events": [{"abi": "erc20", "event": "Transfer", "holder_topics": [1, 2]}]},
    ]}))
    (cfg / "pools.yaml").write_text(yaml.safe_dump({"pools": [EXISTING_POOL]}))
    (cfg / "jobs.yaml").write_text(yaml.safe_dump({"jobs": {
        "daily_cl_liquidity": CL_JOB, "daily_custom": CUSTOM_JOB,
    }}))
    (cfg / "universes.yaml").write_text(yaml.safe_dump({"universes": {
        "full_holders": {"kind": "full_holders"},
        "treasury": {"kind": "explicit_list", "source": "vendored/treasury.csv"},
    }}))
    (out / "pools.json").write_text(json.dumps({"pools": [NEW_POOL]}))
    (out / "tokens.json").write_text(json.dumps({"tokens": [
        {"address": "0x" + "22" * 20, "symbol": "BBB", "decimals": 6,
         "token_class": "standard_erc20", "deployment_block": 5_000_000,
         "discovery_events": [{"abi": "erc20", "event": "Transfer", "holder_topics": [1, 2]}]},
    ]}))
    # no curated_resolved.json -> the curated path is optional for a refresh


def test_assemble_appends_new_pool_without_touching_existing(tmp_path: Path) -> None:
    cfg, out = tmp_path / "gnosis", tmp_path / "out"
    _seed_catalog(cfg, out)

    summary = assemble_mod.assemble(str(cfg), str(out))

    pools = {p["address"]: p for p in yaml.safe_load((cfg / "pools.yaml").read_text())["pools"]}
    assert set(pools) == {EXISTING_POOL["address"], NEW_POOL["address"]}
    assert pools[EXISTING_POOL["address"]] == EXISTING_POOL  # byte-for-byte preserved
    assert pools[NEW_POOL["address"]]["tick_spacing"] == 10  # CL immutables carried through
    assert summary["pools"] == 2


def test_assemble_preserves_cl_and_custom_jobs_and_seeds_standard(tmp_path: Path) -> None:
    cfg, out = tmp_path / "gnosis", tmp_path / "out"
    _seed_catalog(cfg, out)

    assemble_mod.assemble(str(cfg), str(out))

    jobs = yaml.safe_load((cfg / "jobs.yaml").read_text())["jobs"]
    assert jobs["daily_cl_liquidity"] == CL_JOB          # not clobbered
    assert jobs["daily_custom"] == CUSTOM_JOB            # custom job survives
    assert "daily_pool_reserves" in jobs                 # standard jobs seeded
    assert "daily_treasury" in jobs
    # no curated file -> the curated-balances job is neither seeded nor present
    assert "daily_curated_balances" not in jobs


def test_assemble_new_token_appended_existing_kept(tmp_path: Path) -> None:
    cfg, out = tmp_path / "gnosis", tmp_path / "out"
    _seed_catalog(cfg, out)

    assemble_mod.assemble(str(cfg), str(out))

    tokens = {t["address"]: t for t in yaml.safe_load((cfg / "tokens.yaml").read_text())["tokens"]}
    assert set(tokens) == {"0x" + "11" * 20, "0x" + "22" * 20}
    assert tokens["0x" + "11" * 20]["decimals"] == 18  # existing preserved
