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
    RpcFailure,
    classify_rpc_failure,
    normalize_rpc_error,
)
from rpc_state_indexer.rpc.client import AsyncRpcClient
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.errors import RpcNoHealthyEndpoint, RpcProviderLimit


def _first_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The first non-group exception inside a TaskGroup failure."""

    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            return _first_leaf(exc)
        return exc
    return group


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
        # The semaphore bounds in-flight batches without wave barriers (a slow batch no
        # longer holds back the next wave). Results are stored by input index and then
        # flattened, so batch_sequence stays stable. The first failure cancels the
        # remaining batches and propagates as the original exception type.
        limiter = asyncio.Semaphore(self.max_parallel_batches)
        slots: list[list[VerifiedBatchResult] | None] = [None] * len(groups)

        async def run(index: int, group: list[ContractCall]) -> None:
            async with limiter:
                slots[index] = await self._execute_adaptive(group, anchor)

        try:
            async with asyncio.TaskGroup() as tasks:
                for index, group in enumerate(groups):
                    tasks.create_task(run(index, group))
        except BaseExceptionGroup as group_exc:
            raise _first_leaf(group_exc) from group_exc

        output: list[VerifiedBatchResult] = []
        for index, batch_results in enumerate(slots):
            if batch_results is None:
                raise BatchResultCountMismatch(f"batch {index} produced no result")
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
        last_failure: RpcFailure | None = None
        last_endpoint: RpcEndpoint | None = None
        for attempt in range(self.rpc.max_retries):
            try:
                endpoint = await self._select(anchor, excluded)
            except asyncio.CancelledError:
                raise
            except RpcNoHealthyEndpoint as exc:
                # Nothing is selectable: this call's failover exclusions, sibling batches'
                # cooldowns, or archive coverage. After a transient/rate-limit failure the
                # last endpoint is busy, not broken, so retry it (bounded by max_retries)
                # instead of failing the batch. Permanent and archive-unavailable failures
                # fail closed, as does a first attempt with nothing to fall back to. The
                # caller's sentinel exclusions stay in `excluded` and are never reselected.
                if last_endpoint is None or last_failure is None or not last_failure.retryable:
                    last = exc
                    break
                endpoint = last_endpoint
            except BaseException as exc:
                last = exc
                break
            if last_failure is not None:
                await asyncio.sleep(
                    self._retry_delay(
                        attempt, last_failure, same_endpoint=endpoint is last_endpoint
                    )
                )
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
            except asyncio.CancelledError:
                # A sibling batch failed and the TaskGroup cancelled this one: not an
                # endpoint failure, so it must not escalate the endpoint's cooldown.
                raise
            except BaseException as exc:
                normalized = normalize_rpc_error(exc)
                failure = classify_rpc_failure(normalized)
                last = normalized
                last_failure = failure
                last_endpoint = endpoint
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
                continue
            self.rpc.endpoint_pool.record_success(endpoint)
            return result, endpoint, reference_kind
        raise RpcNoHealthyEndpoint("Multicall3 exhausted endpoints") from last

    async def _select(self, anchor: BlockRef, excluded: set[str]) -> RpcEndpoint:
        try:
            return await self.rpc.endpoint_pool.select(
                historical_block=anchor.number,
                require_eip1898=True,
                exclude=frozenset(excluded),
            )
        except RpcNoHealthyEndpoint:
            return await self.rpc.endpoint_pool.select(
                historical_block=anchor.number,
                exclude=frozenset(excluded),
            )

    def _retry_delay(
        self, attempt: int, failure: RpcFailure, *, same_endpoint: bool
    ) -> float:
        # `attempt` is the retry about to run; the failed one was `attempt - 1`.
        # Retry-After is a per-endpoint hint and only applies when that endpoint is
        # reused; a hop to another endpoint keeps the short pre-existing backoff.
        backoff = self.rpc.retry_base_seconds * (2.0 ** (attempt - 1))
        if not same_endpoint:
            return min(2.0, backoff)
        return min(60.0, max(failure.retry_after or 0.0, backoff))

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
