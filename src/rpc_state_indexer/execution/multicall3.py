"""Verified Multicall3 execution with the sentinel triple at both ends."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

from eth_abi.exceptions import DecodingError

from rpc_state_indexer.domain import BlockRef, ExecutorKind
from rpc_state_indexer.errors import BatchVerificationError
from rpc_state_indexer.evm.abi import decode_aggregate3, encode_aggregate3
from rpc_state_indexer.evm.calldata import (
    GET_BLOCK_NUMBER_SELECTOR,
    GET_CURRENT_BLOCK_TIMESTAMP_SELECTOR,
    get_block_hash_calldata,
)
from rpc_state_indexer.evm.decoding import decode_uint256, hex_data_to_bytes
from rpc_state_indexer.execution.base import (
    ContractCall,
    RawCallResult,
    VerificationEvidence,
    VerifiedBatchResult,
    digest_raw_results,
)
from rpc_state_indexer.execution.batch_planner import chunked
from rpc_state_indexer.execution.errors import (
    BatchResultCountMismatch,
    SentinelMismatch,
    UnsupportedExecutionRange,
)
from rpc_state_indexer.execution.verification import (
    assert_anchor_hash,
    eip1898_reference,
    normalize_hash,
    number_reference,
)
from rpc_state_indexer.observability.metrics import (
    BATCH_SENTINEL_FAILURES,
    RPC_BATCH_SECONDS,
)
from rpc_state_indexer.rpc.classification import (
    FailureKind,
    classify_rpc_failure,
    normalize_rpc_error,
)
from rpc_state_indexer.rpc.client import AsyncRpcClient
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.errors import RpcNoHealthyEndpoint, RpcProviderLimit


class Multicall3Executor:
    def __init__(
        self,
        rpc: AsyncRpcClient,
        *,
        address: str,
        deployment_block: int,
        batch_size: int = 250,
        max_parallel_batches: int = 8,
    ) -> None:
        self.rpc = rpc
        self.address = address.lower()
        self.deployment_block = deployment_block
        self.batch_size = batch_size
        if batch_size < 1:
            raise ValueError("batch size must be positive")
        if max_parallel_batches < 1:
            raise ValueError("max parallel batches must be positive")
        self.max_parallel_batches = max_parallel_batches

    async def execute(
        self,
        calls: Sequence[ContractCall],
        anchor: BlockRef,
    ) -> list[VerifiedBatchResult]:
        if anchor.number < self.deployment_block:
            raise UnsupportedExecutionRange("Multicall3 is not deployed at the anchor")
        if anchor.number == 0:
            raise UnsupportedExecutionRange("Multicall sentinels require anchor block > 0")

        groups = list(chunked(calls, self.batch_size))
        if len(groups) == 1:
            return list(await self._execute_adaptive(groups[0], anchor))

        # Batches are independent units of verification: each carries its own
        # block/timestamp/parent-hash sentinels at head and tail and is proven on its own,
        # so running them concurrently cannot weaken any guarantee. Executing them serially
        # left the RPC client's concurrency semaphore idle and made a full-holder census
        # (tens of batches) take tens of seconds of pure round-trip latency.
        #
        # gather preserves input order, which batch_sequence depends on. The wave bound
        # keeps the number of pending coroutines sane for very large universes; actual
        # in-flight requests are already capped by the client's own semaphore.
        output: list[VerifiedBatchResult] = []
        for start in range(0, len(groups), self.max_parallel_batches):
            wave = groups[start : start + self.max_parallel_batches]
            for batch_results in await asyncio.gather(
                *(self._execute_adaptive(group, anchor) for group in wave)
            ):
                output.extend(batch_results)
        return output

    async def _execute_adaptive(
        self,
        calls: list[ContractCall],
        anchor: BlockRef,
    ) -> list[VerifiedBatchResult]:
        started = time.perf_counter()
        try:
            result = await self._execute_once(calls, anchor)
            RPC_BATCH_SECONDS.labels(executor="multicall3").observe(
                time.perf_counter() - started
            )
            return [result]
        except RpcProviderLimit:
            if len(calls) == 1:
                raise
            midpoint = len(calls) // 2
            return await self._execute_adaptive(
                calls[:midpoint], anchor
            ) + await self._execute_adaptive(calls[midpoint:], anchor)

    def _sentinels(self, position: str, anchor: BlockRef) -> list[ContractCall]:
        return [
            ContractCall(
                f"__sentinel_{position}_block",
                self.address,
                GET_BLOCK_NUMBER_SELECTOR,
                False,
            ),
            ContractCall(
                f"__sentinel_{position}_timestamp",
                self.address,
                GET_CURRENT_BLOCK_TIMESTAMP_SELECTOR,
                False,
            ),
            ContractCall(
                f"__sentinel_{position}_parent",
                self.address,
                get_block_hash_calldata(anchor.number - 1),
                False,
            ),
        ]

    async def _eth_call(
        self,
        calldata: bytes,
        anchor: BlockRef,
        *,
        exclude: set[str] | None = None,
    ) -> tuple[str, RpcEndpoint, str]:
        excluded = set(exclude or ())
        last: BaseException | None = None
        for attempt in range(self.rpc.max_retries):
            try:
                try:
                    endpoint = await self.rpc.endpoint_pool.select(
                        historical_block=anchor.number,
                        require_eip1898=True,
                        exclude=frozenset(excluded),
                    )
                except RpcNoHealthyEndpoint:
                    endpoint = await self.rpc.endpoint_pool.select(
                        historical_block=anchor.number,
                        exclude=frozenset(excluded),
                    )
            except BaseException as exc:
                last = exc
                break
            reference_kind = (
                "eip1898" if endpoint.supports_eip1898 else "number_hash_sandwich"
            )
            reference = (
                eip1898_reference(anchor)
                if endpoint.supports_eip1898
                else number_reference(anchor)
            )
            try:
                if not endpoint.supports_eip1898:
                    await assert_anchor_hash(self.rpc, endpoint, anchor)
                result = await self.rpc.call_on_endpoint(
                    endpoint,
                    "eth_call",
                    [{"to": self.address, "data": "0x" + calldata.hex()}, reference],
                )
                if not endpoint.supports_eip1898:
                    await assert_anchor_hash(self.rpc, endpoint, anchor)
                if not isinstance(result, str):
                    raise ValueError("eth_call result must be hex data")
            except BaseException as exc:
                normalized = normalize_rpc_error(exc)
                failure = classify_rpc_failure(normalized)
                last = normalized
                self.rpc.endpoint_pool.record_failure(
                    endpoint,
                    failover=failure.failover,
                    historical_block=anchor.number,
                    archive_unavailable=failure.kind is FailureKind.ARCHIVE_UNAVAILABLE,
                )
                if failure.kind is FailureKind.PROVIDER_LIMIT:
                    raise RpcProviderLimit(str(normalized)) from normalized
                if failure.failover:
                    excluded.add(endpoint.name)
                if not failure.retryable and not failure.failover:
                    raise normalized from exc
                await asyncio.sleep(
                    min(2.0, self.rpc.retry_base_seconds * (2**attempt))
                )
                continue
            self.rpc.endpoint_pool.record_success(endpoint)
            return result, endpoint, reference_kind
        raise RpcNoHealthyEndpoint("Multicall3 exhausted endpoints") from last

    async def _execute_once(
        self,
        calls: list[ContractCall],
        anchor: BlockRef,
        *,
        escalate_failed_subcalls: bool = True,
    ) -> VerifiedBatchResult:
        head = self._sentinels("head", anchor)
        tail = self._sentinels("tail", anchor)
        packed = head + calls + tail
        calldata = encode_aggregate3(
            [(call.target, call.allow_failure, call.calldata) for call in packed]
        )
        excluded: set[str] = set()
        last_verification_error: BaseException | None = None

        for attempt in range(self.rpc.max_retries):
            encoded, endpoint, reference_kind = await self._eth_call(
                calldata, anchor, exclude=excluded
            )
            try:
                decoded = decode_aggregate3(hex_data_to_bytes(encoded))
                if len(decoded) != len(packed):
                    raise BatchResultCountMismatch(
                        f"expected {len(packed)} results, got {len(decoded)}"
                    )
                self._verify_sentinels(decoded[:3], anchor, "head")
                self._verify_sentinels(decoded[-3:], anchor, "tail")
            except (BatchVerificationError, DecodingError, ValueError) as exc:
                last_verification_error = exc
                BATCH_SENTINEL_FAILURES.labels(reason=type(exc).__name__).inc()
                self.rpc.endpoint_pool.record_failure(endpoint, failover=True)
                excluded.add(endpoint.name)
                await asyncio.sleep(
                    min(2.0, self.rpc.retry_base_seconds * (2**attempt))
                )
                continue

            body = [
                RawCallResult(call.key, success, returndata)
                for call, (success, returndata) in zip(
                    calls, decoded[3:-3], strict=True
                )
            ]
            provider_groups = {endpoint.provider_group}
            if escalate_failed_subcalls:
                for index, result in enumerate(body):
                    if result.success:
                        continue
                    retry = await self._execute_once(
                        [calls[index]],
                        anchor,
                        escalate_failed_subcalls=False,
                    )
                    if len(retry.results) != 1:
                        raise BatchResultCountMismatch(
                            "single-call escalation returned the wrong result count"
                        )
                    body[index] = retry.results[0]
                    provider_groups.update(retry.evidence.provider_groups)

            final_body = tuple(body)
            return VerifiedBatchResult(
                final_body,
                VerificationEvidence(
                    ExecutorKind.MULTICALL3,
                    reference_kind,
                    normalize_hash(anchor.block_hash),
                    tuple(sorted(provider_groups)),
                    digest_raw_results(final_body),
                    True,
                ),
            )

        raise BatchVerificationError(
            "Multicall3 could not verify the pinned anchor on any endpoint"
        ) from last_verification_error

    @staticmethod
    def _verify_sentinels(
        results: Sequence[tuple[bool, bytes]],
        anchor: BlockRef,
        position: str,
    ) -> None:
        if len(results) != 3 or not all(success for success, _ in results):
            raise SentinelMismatch(f"{position} sentinel reverted")
        block = decode_uint256(True, results[0][1])
        timestamp = decode_uint256(True, results[1][1])
        if not block.ok or block.value != anchor.number:
            raise SentinelMismatch(f"{position} block sentinel mismatch")
        if not timestamp.ok or timestamp.value != anchor.timestamp:
            raise SentinelMismatch(f"{position} timestamp sentinel mismatch")
        parent = results[2][1]
        if len(parent) != 32 or "0x" + parent.hex() != normalize_hash(anchor.parent_hash):
            raise SentinelMismatch(f"{position} parent-hash sentinel mismatch")
