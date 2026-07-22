"""Fail-closed endpoint capability probes."""

from __future__ import annotations

from dataclasses import dataclass

from eth_utils.crypto import keccak

from rpc_state_indexer.core.anchors import parse_block
from rpc_state_indexer.evm.calldata import GET_BLOCK_NUMBER_SELECTOR
from rpc_state_indexer.evm.decoding import decode_uint256, hex_data_to_bytes
from rpc_state_indexer.rpc.client import AsyncRpcClient
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.errors import RpcResponseError


async def probe_chain_id(
    rpc: AsyncRpcClient,
    endpoint: RpcEndpoint,
    expected_chain_id: int,
) -> None:
    result = await rpc.call_on_endpoint(endpoint, "eth_chainId", [])
    if not isinstance(result, str) or int(result, 16) != expected_chain_id:
        raise ValueError(f"endpoint {endpoint.name} has the wrong chain ID")


async def probe_eip1898(
    rpc: AsyncRpcClient,
    endpoint: RpcEndpoint,
    *,
    call_to: str,
    canonical_block_hash: str,
    canonical_block_number: int,
    calldata: str,
) -> bool:
    """Require a real hash-pinned call to work and a bogus hash to fail.

    A negative-only probe is unsafe: endpoints that reject all EIP-1898 objects with
    ``invalid params`` also reject a bogus hash.  The successful canonical call proves
    support before the negative probe establishes that the endpoint does not silently
    ignore the requested hash.
    """

    try:
        canonical_result = await rpc.call_on_endpoint(
            endpoint,
            "eth_call",
            [
                {"to": call_to, "data": calldata},
                {"blockHash": canonical_block_hash, "requireCanonical": True},
            ],
        )
    except RpcResponseError:
        endpoint.supports_eip1898 = False
        return False
    if not isinstance(canonical_result, str):
        endpoint.supports_eip1898 = False
        return False
    observed_block = decode_uint256(True, canonical_result)
    if not observed_block.ok or observed_block.value != canonical_block_number:
        endpoint.supports_eip1898 = False
        return False

    bogus_hash = "0x" + "ff" * 32
    try:
        await rpc.call_on_endpoint(
            endpoint,
            "eth_call",
            [
                {"to": call_to, "data": calldata},
                {"blockHash": bogus_hash, "requireCanonical": True},
            ],
        )
    except RpcResponseError:
        endpoint.supports_eip1898 = True
        return True
    endpoint.supports_eip1898 = False
    return False


async def probe_eip1898_negative(
    rpc: AsyncRpcClient,
    endpoint: RpcEndpoint,
    *,
    call_to: str,
    calldata: str = "0x",
) -> bool:
    """Deprecated negative-only compatibility helper.

    Kept for callers outside the package, but intentionally never marks an endpoint
    safe.  Runtime setup must use :func:`probe_eip1898` with a canonical block hash.
    """

    del rpc, endpoint, call_to, calldata
    return False


@dataclass(frozen=True, slots=True)
class EndpointCapabilities:
    chain_id_verified: bool
    supports_http_batch: bool
    supports_eip1898: bool
    supports_finality_tag: bool
    archive_from_block: int
    multicall_code_hash_verified: bool


async def _probe_http_batch(
    rpc: AsyncRpcClient,
    endpoint: RpcEndpoint,
    expected_chain_id: int,
) -> bool:
    requests = [
        {"jsonrpc": "2.0", "id": 7001, "method": "eth_chainId", "params": []},
        {"jsonrpc": "2.0", "id": 7002, "method": "eth_chainId", "params": []},
    ]
    try:
        responses = await rpc.batch_on_endpoint(endpoint, requests)
    except Exception:
        endpoint.supports_http_batch = False
        return False
    by_id = {
        item.get("id"): item.get("result")
        for item in responses
        if item.get("id") in {7001, 7002}
    }
    expected = hex(expected_chain_id)
    supported = by_id == {7001: expected, 7002: expected}
    endpoint.supports_http_batch = supported
    return supported


async def probe_archive_state(
    rpc: AsyncRpcClient,
    endpoint: RpcEndpoint,
    *,
    contract_address: str,
    block_number: int,
    calldata: str,
) -> None:
    """Prove that an endpoint can read the earliest configured contract state.

    Reading only Multicall3's deployment block is insufficient for pre-Multicall
    backfills. This probe requires the earliest configured token's block, runtime
    bytecode, and one strictly decoded uint256 call to all be available.
    """

    if block_number < 0:
        raise ValueError("archive probe block cannot be negative")
    block_raw = await rpc.call_on_endpoint(
        endpoint,
        "eth_getBlockByNumber",
        [hex(block_number), False],
    )
    block = parse_block(block_raw, expected_number=block_number)
    reference: object = (
        {"blockHash": block.block_hash, "requireCanonical": True}
        if endpoint.supports_eip1898
        else hex(block_number)
    )
    code_raw = await rpc.call_on_endpoint(
        endpoint,
        "eth_getCode",
        [contract_address, reference],
    )
    if not isinstance(code_raw, str):
        raise ValueError("archive probe eth_getCode result is not hex")
    code = hex_data_to_bytes(code_raw)
    if not code:
        raise ValueError("archive probe contract has no code at configured start block")

    result = await rpc.call_on_endpoint(
        endpoint,
        "eth_call",
        [{"to": contract_address, "data": calldata}, reference],
    )
    observation = decode_uint256(True, result) if isinstance(result, str) else None
    if observation is None or not observation.ok:
        detail = "non-hex result" if observation is None else observation.status.value
        raise ValueError(f"archive probe state call failed strict decoding: {detail}")
    endpoint.archive_from_block = block_number


async def probe_endpoint_capabilities(
    rpc: AsyncRpcClient,
    endpoint: RpcEndpoint,
    *,
    expected_chain_id: int,
    finality_tag: str,
    multicall_address: str,
    multicall_deployment_block: int,
    expected_multicall_code_hash: str,
    archive_probe_address: str,
    archive_probe_block: int,
    archive_probe_calldata: str,
) -> EndpointCapabilities:
    """Probe the safety properties used by historical execution.

    Multicall bytecode is pinned at its deployment block, then a separate state probe
    proves archive availability at the earliest configured token block.
    """

    await probe_chain_id(rpc, endpoint, expected_chain_id)
    supports_http_batch = await _probe_http_batch(
        rpc, endpoint, expected_chain_id
    )

    try:
        finalized = await rpc.call_on_endpoint(
            endpoint, "eth_getBlockByNumber", [finality_tag, False]
        )
        parse_block(finalized)
    except (RpcResponseError, ValueError):
        supports_finality_tag = False
    else:
        supports_finality_tag = True

    deployment_raw = await rpc.call_on_endpoint(
        endpoint,
        "eth_getBlockByNumber",
        [hex(multicall_deployment_block), False],
    )
    deployment = parse_block(
        deployment_raw, expected_number=multicall_deployment_block
    )
    code_raw = await rpc.call_on_endpoint(
        endpoint,
        "eth_getCode",
        [multicall_address, hex(multicall_deployment_block)],
    )
    if not isinstance(code_raw, str):
        raise ValueError("eth_getCode result is not hex")
    code = hex_data_to_bytes(code_raw)
    if not code:
        raise ValueError("Multicall3 is not deployed at configured deployment block")
    actual_code_hash = "0x" + keccak(code).hex()
    expected_hash = expected_multicall_code_hash.lower()
    if actual_code_hash != expected_hash:
        raise ValueError(
            f"Multicall3 runtime hash mismatch: {actual_code_hash}"
        )

    supports_eip1898 = await probe_eip1898(
        rpc,
        endpoint,
        call_to=multicall_address,
        canonical_block_hash=deployment.block_hash,
        canonical_block_number=deployment.number,
        calldata="0x" + GET_BLOCK_NUMBER_SELECTOR.hex(),
    )
    await probe_archive_state(
        rpc,
        endpoint,
        contract_address=archive_probe_address,
        block_number=archive_probe_block,
        calldata=archive_probe_calldata,
    )
    return EndpointCapabilities(
        chain_id_verified=True,
        supports_http_batch=supports_http_batch,
        supports_eip1898=supports_eip1898,
        supports_finality_tag=supports_finality_tag,
        archive_from_block=archive_probe_block,
        multicall_code_hash_verified=True,
    )
