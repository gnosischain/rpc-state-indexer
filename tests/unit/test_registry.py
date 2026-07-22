from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from rpc_state_indexer.config.hashing import canonical_hash
from rpc_state_indexer.config.loader import load_catalog
from rpc_state_indexer.core.census import CatalogRegistrar

ROOT = Path(__file__).parents[2]
WETH = "0x6a023ccd1ff6f2045c3309768ead9e68f978f6e1"


class RegistryStore:
    def __init__(self) -> None:
        self.configs: list[dict[str, Any]] = []

    def register_configs(self, rows: list[dict[str, Any]]) -> int:
        self.configs.extend(rows)
        return len(rows)


def test_registry_config_document_matches_hash_and_entity_window() -> None:
    catalog = load_catalog(ROOT / "config", "gnosis")
    tokens = dict(catalog.tokens)
    tokens[WETH] = tokens[WETH].model_copy(update={"date_end": date(2026, 1, 2)})
    catalog = replace(catalog, tokens=tokens)
    store = RegistryStore()

    CatalogRegistrar(store, catalog).register()  # type: ignore[arg-type]

    rows = [row for row in store.configs if row["target_address"] == WETH]
    assert rows
    for row in rows:
        assert row["coverage_start"] == date(2020, 8, 19)
        assert row["coverage_end"] == date(2026, 1, 2)
        assert canonical_hash(json.loads(row["canonical_config_json"])) == row["config_hash"]

