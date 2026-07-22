from pathlib import Path

import pytest

from rpc_state_indexer.evm.events import AbiDefinitionError, AbiRegistry

ABI_ROOT = Path(__file__).parents[2] / "abis"


def test_transfer_topic_is_derived_from_committed_abi() -> None:
    event = AbiRegistry(ABI_ROOT).validate_holder_topics(
        "erc20", "Transfer", [1, 2]
    )
    assert event.topic0 == (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )
    assert event.indexed_types == ("address", "address")


def test_holder_topic_must_exist_and_be_an_address() -> None:
    registry = AbiRegistry(ABI_ROOT)
    with pytest.raises(AbiDefinitionError, match="no indexed topic 2"):
        registry.validate_holder_topics("weth9", "Deposit", [2])
