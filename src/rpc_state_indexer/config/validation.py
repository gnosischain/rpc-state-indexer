from pathlib import Path

from rpc_state_indexer.config.loader import Catalog
from rpc_state_indexer.errors import ConfigError
from rpc_state_indexer.evm.events import AbiDefinitionError, AbiRegistry


def validate_runtime_catalog(catalog: Catalog, abi_root: Path) -> None:
    registry = AbiRegistry(abi_root)
    for token in catalog.tokens.values():
        for event in token.discovery_events:
            try:
                registry.validate_holder_topics(
                    event.abi, event.event, event.holder_topics
                )
            except AbiDefinitionError as exc:
                raise ConfigError(
                    f"invalid discovery ABI for {token.symbol}/{event.event}: {exc}"
                ) from exc
    if catalog.chain.multicall3.runtime_code_hash is None:
        raise ConfigError("multicall3.runtime_code_hash must be pinned before RPC execution")
