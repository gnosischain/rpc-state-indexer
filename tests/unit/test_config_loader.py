from pathlib import Path

import pytest

from rpc_state_indexer.config.loader import _read_yaml
from rpc_state_indexer.config.models import UniverseConfig
from rpc_state_indexer.errors import ConfigError


def test_yaml_duplicate_keys_are_rejected(tmp_path: Path) -> None:
    config = tmp_path / "duplicate.yaml"
    config.write_text("jobs:\n  daily: 1\n  daily: 2\n")

    with pytest.raises(ConfigError, match="duplicate key"):
        _read_yaml(config)


@pytest.mark.parametrize(
    "source",
    ["../addresses.csv", "/tmp/addresses.csv", "addresses.csv", "vendored/a.txt"],
)
def test_explicit_universe_must_use_vendored_csv(source: str) -> None:
    with pytest.raises(ValueError, match=r"vendored/\*\.csv"):
        UniverseConfig(kind="explicit_list", source=source)
