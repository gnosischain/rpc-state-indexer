from __future__ import annotations

import os
from pathlib import Path

import pytest

from rpc_state_indexer.config.loader import load_catalog
from rpc_state_indexer.core.anchors import parse_block
from rpc_state_indexer.domain import ExecutorKind
from rpc_state_indexer.evm.calldata import TOTAL_SUPPLY_SELECTOR
from rpc_state_indexer.evm.decoding import decode_uint256
from rpc_state_indexer.execution.base import ContractCall
from rpc_state_indexer.rpc.capabilities import probe_endpoint_capabilities
from rpc_state_indexer.runtime import build_rpc_runtime
from rpc_state_indexer.settings import RuntimeSettings

ROOT = Path(__file__).parents[2]


@pytest.mark.pinned_chain
@pytest.mark.parametrize(
    ("block_number", "expected_executor"),
    [
        (20_000_000, ExecutorKind.LEGACY_RPC_BATCH),
        (21_022_500, ExecutorKind.MULTICALL3),
    ],
)
async def test_state_calls_on_both_sides_of_multicall_deployment(
    block_number: int, expected_executor: ExecutorKind
) -> None:
    urls = os.getenv("GNOSIS_ARCHIVE_RPC_URLS", "")
    if not urls:
        pytest.skip("GNOSIS_ARCHIVE_RPC_URLS is not configured")
    groups = os.getenv("GNOSIS_ARCHIVE_PROVIDER_GROUPS", "")
    settings = RuntimeSettings(
        RPC_URLS=urls,
        RPC_PROVIDER_GROUPS=groups,
        CONFIG_ROOT=ROOT / "config",
        ABI_ROOT=ROOT / "abis",
    )
    catalog = load_catalog(settings.config_root, "gnosis")
    runtime = build_rpc_runtime(settings, catalog)
    try:
        multicall = catalog.chain.multicall3
        archive_token = min(
            catalog.tokens.values(),
            key=lambda token: (token.deployment_block, token.address),
        )
        assert multicall.runtime_code_hash is not None
        for endpoint in runtime.rpc.endpoint_pool.endpoints:
            await probe_endpoint_capabilities(
                runtime.rpc,
                endpoint,
                expected_chain_id=100,
                finality_tag="finalized",
                multicall_address=multicall.address,
                multicall_deployment_block=multicall.deployment_block,
                expected_multicall_code_hash=multicall.runtime_code_hash,
                archive_probe_address=archive_token.address,
                archive_probe_block=archive_token.deployment_block,
                archive_probe_calldata="0x" + TOTAL_SUPPLY_SELECTOR.hex(),
            )
        raw, _ = await runtime.rpc.call(
            "eth_getBlockByNumber",
            [hex(block_number), False],
            historical_block=block_number,
        )
        anchor = parse_block(raw, expected_number=block_number)
        token = catalog.tokens[
            "0xe91d153e0b41518a2ce8dd3d7944fa863463a97d"
        ]
        batches = await runtime.executor.execute(
            [ContractCall("totalSupply", token.address, TOTAL_SUPPLY_SELECTOR)],
            anchor,
        )
        assert len(batches) == 1
        assert batches[0].evidence.executor_kind is expected_executor
        assert batches[0].evidence.verified is True
        assert len(batches[0].results) == 1
        observation = decode_uint256(
            batches[0].results[0].success,
            batches[0].results[0].returndata,
        )
        assert observation.ok
        assert observation.value is not None and observation.value > 0
    finally:
        await runtime.close()
