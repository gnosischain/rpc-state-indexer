"""Pre-Multicall historical calls via EIP-1898 or provider quorum."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

from rpc_state_indexer.domain import BlockRef, ExecutorKind
from rpc_state_indexer.evm.decoding import hex_data_to_bytes
from rpc_state_indexer.observability.metrics import RPC_BATCH_SECONDS
from rpc_state_indexer.rpc.classification import (
    FailureKind,
    classify_rpc_failure,
    normalize_rpc_error,
)
from rpc_state_indexer.rpc.client import AsyncRpcClient
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.errors import (
    RpcNoHealthyEndpoint,
    RpcProviderLimit,
    RpcResponseError,
)

from .base import (
    ContractCall,
    RawCallResult,
    VerificationEvidence,
    VerifiedBatchResult,
    digest_raw_results,
)
from .batch_planner import chunked
from .errors import (
    DuplicateBatchResponseId,
    MalformedBatchResponse,
    MissingBatchResponses,
    ProviderQuorumMismatch,
    UnknownBatchResponseId,
)
from .verification import (
    assert_anchor_hash,
    eip1898_reference,
    normalize_hash,
    number_reference,
)

JsonObject = dict[str, Any]


def build_batch_requests(
    calls: Sequence[ContractCall], anchor: BlockRef, *, eip1898: bool
) -> tuple[list[JsonObject], dict[int, ContractCall]]:
    """Build a deterministic JSON-RPC batch and its response-ID map."""
    requests: list[JsonObject] = []
    calls_by_id: dict[int, ContractCall] = {}
    reference: object = (
        eip1898_reference(anchor) if eip1898 else number_reference(anchor)
    )
    for request_id, call in enumerate(calls, start=1):
        requests.append(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "eth_call",
                "params": [
                    {"to": call.target, "data": "0x" + call.calldata.hex()},
                    reference,
                ],
            }
        )
        calls_by_id[request_id] = call
    return requests, calls_by_id


def decode_batch_responses(
    responses: Sequence[Mapping[str, Any]],
    calls_by_id: Mapping[int, ContractCall],
) -> tuple[RawCallResult, ...]:
    """Decode a JSON-RPC batch without relying on response ordering."""
    by_id: dict[int, Mapping[str, Any]] = {}
    for response in responses:
        request_id = response.get("id")
        if type(request_id) is not int or request_id not in calls_by_id:
            raise UnknownBatchResponseId(f"unknown response ID {request_id!r}")
        if request_id in by_id:
            raise DuplicateBatchResponseId(f"duplicate response ID {request_id}")
        if response.get("jsonrpc") not in {None, "2.0"}:
            raise MalformedBatchResponse("unexpected JSON-RPC version")
        by_id[request_id] = response

    missing = sorted(set(calls_by_id) - set(by_id))
    if missing:
        raise MissingBatchResponses(f"missing response IDs: {missing}")

    output: list[RawCallResult] = []
    for request_id in sorted(calls_by_id):
        call = calls_by_id[request_id]
        response = by_id[request_id]
        has_result = "result" in response
        has_error = "error" in response
        if has_result == has_error:
            raise MalformedBatchResponse(
                f"response {request_id} must contain exactly one of result/error"
            )
        if has_error:
            error = response["error"]
            if not isinstance(error, Mapping):
                raise MalformedBatchResponse(
                    f"response {request_id} error is not an object"
                )
            code = error.get("code")
            message = error.get("message")
            if type(code) is not int or not isinstance(message, str):
                raise MalformedBatchResponse(
                    f"response {request_id} has malformed error"
                )
            output.append(RawCallResult(call.key, False, b"", code, message))
            continue

        encoded = response["result"]
        if not isinstance(encoded, str):
            raise MalformedBatchResponse(f"response {request_id} result is not hex")
        try:
            raw = hex_data_to_bytes(encoded)
        except ValueError as exc:
            raise MalformedBatchResponse(
                f"response {request_id} result is malformed"
            ) from exc
        output.append(RawCallResult(call.key, True, raw))
    return tuple(output)


class LegacyRpcBatchExecutor:
    """Execute pre-Multicall calls using hash pinning or provider quorum."""

    def __init__(
        self,
        rpc: AsyncRpcClient,
        *,
        batch_size: int = 100,
        required_provider_quorum: int = 2,
    ) -> None:
        if batch_size < 1 or required_provider_quorum < 2:
            raise ValueError(
                "legacy batch size must be positive and quorum at least two"
            )
        self.rpc = rpc
        self.batch_size = batch_size
        self.required_provider_quorum = required_provider_quorum

    async def execute(
        self, calls: Sequence[ContractCall], anchor: BlockRef
    ) -> list[VerifiedBatchResult]:
        output: list[VerifiedBatchResult] = []
        for group in chunked(calls, self.batch_size):
            output.extend(await self._execute_adaptive(group, anchor))
        return output

    async def _execute_adaptive(
        self, calls: list[ContractCall], anchor: BlockRef
    ) -> list[VerifiedBatchResult]:
        started = time.perf_counter()
        try:
            result = await self._execute_once(calls, anchor)
            RPC_BATCH_SECONDS.labels(executor="legacy_rpc_batch").observe(
                time.perf_counter() - started
            )
            return [result]
        except BaseException as exc:
            normalized = normalize_rpc_error(exc)
            if classify_rpc_failure(normalized).kind is not FailureKind.PROVIDER_LIMIT:
                raise normalized from exc
            if len(calls) == 1:
                raise RpcProviderLimit(str(normalized)) from normalized
            midpoint = len(calls) // 2
            return await self._execute_adaptive(
                calls[:midpoint], anchor
            ) + await self._execute_adaptive(calls[midpoint:], anchor)

    async def _execute_once(
        self, calls: list[ContractCall], anchor: BlockRef
    ) -> VerifiedBatchResult:
        excluded: set[str] = set()
        last_error: BaseException | None = None
        for attempt in range(self.rpc.max_retries):
            try:
                endpoint = await self.rpc.endpoint_pool.select(
                    historical_block=anchor.number,
                    require_eip1898=True,
                    exclude=frozenset(excluded),
                )
            except RpcNoHealthyEndpoint:
                break
            try:
                result = await self._execute_eip1898(endpoint, calls, anchor)
            except BaseException as exc:
                normalized = normalize_rpc_error(exc)
                failure = classify_rpc_failure(normalized)
                last_error = normalized
                self.rpc.endpoint_pool.record_failure(
                    endpoint,
                    failover=failure.failover,
                    historical_block=anchor.number,
                    archive_unavailable=(
                        failure.kind is FailureKind.ARCHIVE_UNAVAILABLE
                    ),
                )
                if failure.kind is FailureKind.PROVIDER_LIMIT:
                    raise RpcProviderLimit(str(normalized)) from normalized
                if not failure.failover and not failure.retryable:
                    raise normalized from exc
                excluded.add(endpoint.name)
                await asyncio.sleep(
                    min(2.0, self.rpc.retry_base_seconds * (2**attempt))
                )
                continue
            self.rpc.endpoint_pool.record_success(endpoint)
            return result

        try:
            return await self._execute_number_quorum(calls, anchor)
        except RpcNoHealthyEndpoint:
            if last_error is not None:
                raise RpcNoHealthyEndpoint(
                    "no verified legacy execution path is available"
                ) from last_error
            raise

    async def _execute_eip1898(
        self,
        endpoint: RpcEndpoint,
        calls: list[ContractCall],
        anchor: BlockRef,
    ) -> VerifiedBatchResult:
        results = await self._run_on_endpoint(
            endpoint, calls, anchor, eip1898=True
        )
        digest = digest_raw_results(results)
        return VerifiedBatchResult(
            results,
            VerificationEvidence(
                ExecutorKind.LEGACY_RPC_BATCH,
                "eip1898",
                normalize_hash(anchor.block_hash),
                (endpoint.provider_group,),
                digest,
                True,
            ),
        )

    async def _execute_number_quorum(
        self, calls: list[ContractCall], anchor: BlockRef
    ) -> VerifiedBatchResult:
        endpoints = self.rpc.endpoint_pool.select_distinct_groups(
            self.required_provider_quorum, historical_block=anchor.number
        )
        executions = await asyncio.gather(
            *(
                self._run_number_sandwich(endpoint, calls, anchor)
                for endpoint in endpoints
            )
        )
        digests = {digest_raw_results(results) for results in executions}
        if len(digests) != 1:
            raise ProviderQuorumMismatch(
                "legacy providers returned different state"
            )
        first = executions[0]
        digest = digest_raw_results(first)
        return VerifiedBatchResult(
            first,
            VerificationEvidence(
                ExecutorKind.LEGACY_RPC_BATCH,
                "number_quorum",
                normalize_hash(anchor.block_hash),
                tuple(endpoint.provider_group for endpoint in endpoints),
                digest,
                True,
            ),
        )

    async def _run_number_sandwich(
        self,
        endpoint: RpcEndpoint,
        calls: list[ContractCall],
        anchor: BlockRef,
    ) -> tuple[RawCallResult, ...]:
        await assert_anchor_hash(self.rpc, endpoint, anchor)
        results = await self._run_on_endpoint(
            endpoint, calls, anchor, eip1898=False
        )
        await assert_anchor_hash(self.rpc, endpoint, anchor)
        return results

    async def _run_on_endpoint(
        self,
        endpoint: RpcEndpoint,
        calls: list[ContractCall],
        anchor: BlockRef,
        *,
        eip1898: bool,
    ) -> tuple[RawCallResult, ...]:
        if endpoint.supports_http_batch:
            requests, calls_by_id = build_batch_requests(
                calls, anchor, eip1898=eip1898
            )
            responses = await self.rpc.batch_on_endpoint(endpoint, requests)
            results = decode_batch_responses(responses, calls_by_id)
        else:
            reference: object = (
                eip1898_reference(anchor)
                if eip1898
                else number_reference(anchor)
            )

            async def one(call: ContractCall) -> RawCallResult:
                try:
                    value = await self.rpc.call_on_endpoint(
                        endpoint,
                        "eth_call",
                        [
                            {
                                "to": call.target,
                                "data": "0x" + call.calldata.hex(),
                            },
                            reference,
                        ],
                    )
                except RpcResponseError as exc:
                    return RawCallResult(
                        call.key, False, b"", exc.code, exc.message
                    )
                if not isinstance(value, str):
                    raise MalformedBatchResponse(
                        "individual eth_call result is not hex"
                    )
                try:
                    raw = hex_data_to_bytes(value)
                except ValueError as exc:
                    raise MalformedBatchResponse(
                        "individual eth_call result is malformed"
                    ) from exc
                return RawCallResult(call.key, True, raw)

            results = tuple(await asyncio.gather(*(one(call) for call in calls)))

        for result in results:
            if result.success:
                continue
            probe = RpcResponseError(
                result.error_code or -1, result.error_message or ""
            )
            if classify_rpc_failure(probe).kind is FailureKind.PROVIDER_LIMIT:
                raise RpcProviderLimit(str(probe))
        return results
