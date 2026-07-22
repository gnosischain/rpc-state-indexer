"""Historical bytecode preconditions for every state-reading target."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from eth_utils.crypto import keccak

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.errors import ArchiveStateUnavailable
from rpc_state_indexer.evm.decoding import hex_data_to_bytes
from rpc_state_indexer.rpc.client import AsyncRpcClient
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.errors import RpcNoHealthyEndpoint

from .verification import assert_anchor_hash, eip1898_reference, number_reference


class HistoricalCodeError(RuntimeError):
    """Historical code could not be proven at the requested anchor."""


class ContractNotDeployed(HistoricalCodeError):
    """The target has no runtime code at the anchor."""


class RuntimeCodeHashMismatch(HistoricalCodeError):
    """Runtime bytecode differs from the pinned hash."""


@dataclass(frozen=True, slots=True)
class CodeEvidence:
    address: str
    code_hash: str
    provider_groups: tuple[str, ...]
    reference_kind: str


class HistoricalCodeVerifier:
    """Read historical bytecode using EIP-1898 or distinct-provider quorum."""

    def __init__(self, rpc: AsyncRpcClient, *, number_provider_quorum: int = 2) -> None:
        if number_provider_quorum < 2:
            raise ValueError("number-provider quorum must be at least two")
        self.rpc = rpc
        self.number_provider_quorum = number_provider_quorum

    async def verify(
        self,
        address: str,
        anchor: BlockRef,
        *,
        expected_code_hash: str | None = None,
    ) -> CodeEvidence:
        normalized = address.lower()
        try:
            endpoint = await self.rpc.endpoint_pool.select(
                historical_block=anchor.number, require_eip1898=True
            )
        except RpcNoHealthyEndpoint:
            return await self._verify_number_quorum(
                normalized, anchor, expected_code_hash
            )

        encoded = await self.rpc.call_on_endpoint(
            endpoint,
            "eth_getCode",
            [normalized, eip1898_reference(anchor)],
        )
        code = self._decode_code(encoded, normalized)
        code_hash = self._check_hash(code, normalized, expected_code_hash)
        return CodeEvidence(
            normalized,
            code_hash,
            (endpoint.provider_group,),
            "eip1898",
        )

    async def _verify_number_quorum(
        self,
        address: str,
        anchor: BlockRef,
        expected_code_hash: str | None,
    ) -> CodeEvidence:
        endpoints = self.rpc.endpoint_pool.select_distinct_groups(
            self.number_provider_quorum, historical_block=anchor.number
        )

        async def read(endpoint: RpcEndpoint) -> bytes:
            await assert_anchor_hash(self.rpc, endpoint, anchor)
            encoded = await self.rpc.call_on_endpoint(
                endpoint,
                "eth_getCode",
                [address, number_reference(anchor)],
            )
            await assert_anchor_hash(self.rpc, endpoint, anchor)
            return self._decode_code(encoded, address)

        values = await asyncio.gather(*(read(endpoint) for endpoint in endpoints))
        if any(value != values[0] for value in values[1:]):
            raise ArchiveStateUnavailable(
                f"providers disagree on historical code for {address}"
            )
        code_hash = self._check_hash(values[0], address, expected_code_hash)
        return CodeEvidence(
            address,
            code_hash,
            tuple(endpoint.provider_group for endpoint in endpoints),
            "number_quorum",
        )

    @staticmethod
    def _decode_code(encoded: object, address: str) -> bytes:
        if not isinstance(encoded, str):
            raise HistoricalCodeError(f"eth_getCode for {address} was not hex data")
        try:
            code = hex_data_to_bytes(encoded)
        except ValueError as exc:
            raise HistoricalCodeError(
                f"eth_getCode for {address} returned malformed hex"
            ) from exc
        if not code:
            raise ContractNotDeployed(f"no code at {address}")
        return code

    @staticmethod
    def _check_hash(
        code: bytes, address: str, expected_code_hash: str | None
    ) -> str:
        actual = "0x" + keccak(code).hex()
        if expected_code_hash is not None and actual != expected_code_hash.lower():
            raise RuntimeCodeHashMismatch(
                f"runtime code hash mismatch for {address}: {actual}"
            )
        return actual
