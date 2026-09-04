"""Disabled catalog entries are dropped from every selector kind.

The explicit-address selector used to take `self.tokens[address]` without checking
`enabled`, so a disabled token still reached the census through jobs that list it by
address (daily_curated_balances) and failed there on every run. Five entries are
disabled in config because the contracts do not exist on Gnosis — no code, no events
ever — and this pins that none of them can become a target through any job.
"""

from pathlib import Path

from rpc_state_indexer.config.loader import load_catalog
from rpc_state_indexer.config.models import PoolSelector, TokenSelector

ROOT = Path(__file__).parents[2]

DEAD_TOKENS = {
    "0x8ad3c73f833d3f9a523ab01476625f269aeb7cf0",  # TSLAX
    "0x78dbaef6b63f8a772dce51f0fb45510fdf286598",  # '?' asset of a uniswap_v3 pool
    "0x7943ad9681f94adbb80cf7cf899508c32f39236a",  # '?' asset of a uniswap_v3 pool
}
DEAD_POOLS = {
    "0x6df2d655927a1df83d1a4785fbab23be5ab79e58",
    "0xf2c2cb6bc3941e4e58cf3466d2856971d25fe034",
}


def test_dead_entries_are_disabled_in_config() -> None:
    catalog = load_catalog(ROOT / "config", "gnosis")
    assert all(not catalog.tokens[a].enabled for a in DEAD_TOKENS)
    assert all(not catalog.pools[a].enabled for a in DEAD_POOLS)


def test_disabled_entries_reach_no_job_through_any_selector_kind() -> None:
    catalog = load_catalog(ROOT / "config", "gnosis")
    # explicit addresses (TSLAX is listed here by address)
    curated = catalog.jobs["daily_curated_balances"]
    assert curated.token_selector is not None
    assert "0x8ad3c73f833d3f9a523ab01476625f269aeb7cf0" in curated.token_selector.addresses
    assert not DEAD_TOKENS & {t.address for t in catalog.token_targets(curated)}
    # class_in
    supply = catalog.jobs["daily_token_supply"]
    assert not DEAD_TOKENS & {t.address for t in catalog.token_targets(supply)}
    cl = catalog.jobs["daily_cl_liquidity"]
    assert not DEAD_POOLS & {p.address for p in catalog.pool_targets(cl)}
    # all_enabled
    reserves = catalog.jobs["daily_pool_reserves"]
    assert not DEAD_POOLS & {p.address for p in catalog.pool_targets(reserves)}


def test_explicit_token_list_drops_disabled_entries() -> None:
    catalog = load_catalog(ROOT / "config", "gnosis")
    live = "0x9c58bacc331c9aa871afd802db6379a98e80cedb"  # GNO
    dead = "0x8ad3c73f833d3f9a523ab01476625f269aeb7cf0"  # TSLAX, disabled
    job = catalog.jobs["daily_curated_balances"].model_copy(
        update={"token_selector": TokenSelector(addresses=[live, dead])}
    )
    assert [t.address for t in catalog.token_targets(job)] == [live]


def test_explicit_pool_list_drops_disabled_entries() -> None:
    catalog = load_catalog(ROOT / "config", "gnosis")
    dead = "0x6df2d655927a1df83d1a4785fbab23be5ab79e58"
    live = next(a for a, p in catalog.pools.items() if p.enabled)
    job = catalog.jobs["daily_pool_reserves"].model_copy(
        update={"pool_selector": PoolSelector(addresses=[live, dead])}
    )
    assert [p.address for p in catalog.pool_targets(job)] == [live]
