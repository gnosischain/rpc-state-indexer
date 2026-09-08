import asyncio
from collections.abc import Sequence
from typing import Any, cast

import pytest
from eth_abi.abi import decode as abi_decode
from eth_abi.abi import encode as abi_encode

from rpc_state_indexer.collectors.common import UIntCallSpec, execute_uint_calls
from rpc_state_indexer.domain import BlockRef, ExecutorKind
from rpc_state_indexer.evm.abi import encode_aggregate3
from rpc_state_indexer.evm.calldata import (
    AGGREGATE3_SELECTOR,
    GET_BLOCK_NUMBER_SELECTOR,
)
from rpc_state_indexer.execution.base import (
    ContractCall,
    RawCallResult,
    VerificationEvidence,
    VerifiedBatchResult,
    digest_raw_results,
)
from rpc_state_indexer.execution.errors import SentinelMismatch
from rpc_state_indexer.execution.multicall3 import Multicall3Executor
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.endpoint_pool import EndpointPool
from rpc_state_indexer.rpc.errors import (
    RpcHttpError,
    RpcNoHealthyEndpoint,
    RpcResponseError,
)

ANCHOR = BlockRef(100, "0x" + "11" * 32, "0x" + "22" * 32, 123456)


def word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def sentinels(
    block: int = 100,
    timestamp: int = 123456,
    parent: bytes = bytes.fromhex("22" * 32),
) -> tuple[tuple[bool, bytes], ...]:
    return ((True, word(block)), (True, word(timestamp)), (True, parent))


def test_aggregate3_encoding_uses_canonical_selector_and_tuple_shape() -> None:
    target = "0x" + "ab" * 20
    encoded = encode_aggregate3([(target, False, GET_BLOCK_NUMBER_SELECTOR)])
    assert encoded[:4] == AGGREGATE3_SELECTOR
    (calls,) = abi_decode(["(address,bool,bytes)[]"], encoded[4:])
    assert calls[0][0].lower() == target
    assert calls[0][1] is False
    assert calls[0][2] == GET_BLOCK_NUMBER_SELECTOR


def test_sentinel_triple_accepts_exact_anchor() -> None:
    Multicall3Executor._verify_sentinels(sentinels(), ANCHOR, "head")


@pytest.mark.parametrize(
    "values",
    [
        sentinels(block=101),
        sentinels(timestamp=123457),
        sentinels(parent=b"\x33" * 32),
        ((False, b""),) + sentinels()[1:],
    ],
)
def test_sentinel_triple_fails_closed(
    values: tuple[tuple[bool, bytes], ...],
) -> None:
    with pytest.raises(SentinelMismatch):
        Multicall3Executor._verify_sentinels(values, ANCHOR, "tail")


# --------------------------------------------------- batch parallelism (execute)


def _fake_batch(index: int) -> object:
    """Stand-in for a VerifiedBatchResult; execute() only orders and concatenates."""

    return f"batch-{index}"


def _executor(*, batch_size: int, max_parallel: int) -> Multicall3Executor:
    return Multicall3Executor(
        cast(Any, object()),
        address="0x" + "ca" * 20,
        deployment_block=0,
        batch_size=batch_size,
        max_parallel_batches=max_parallel,
    )


def _calls(count: int) -> list[ContractCall]:
    return [ContractCall(f"k{i}", "0x" + "11" * 20, b"\x00" * 4) for i in range(count)]


@pytest.mark.asyncio
async def test_independent_batches_run_concurrently() -> None:
    """Each batch proves itself with its own sentinels, so they need not be serialised.

    Running them one at a time left the RPC client's concurrency semaphore idle and made a
    full-holder census cost tens of seconds of pure round-trip latency.
    """

    subject = _executor(batch_size=1, max_parallel=4)
    in_flight = 0
    peak = 0

    async def fake_adaptive(group, anchor):  # type: ignore[no-untyped-def]
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)  # yield so siblings can start
        in_flight -= 1
        return [group[0].key]

    subject._execute_adaptive = fake_adaptive  # type: ignore[method-assign]
    result = cast(Any, await subject.execute(_calls(4), ANCHOR))

    assert peak > 1, "batches were still executed one at a time"
    assert result == ["k0", "k1", "k2", "k3"], "gather must preserve batch order"


@pytest.mark.asyncio
async def test_parallelism_is_bounded_by_max_parallel_batches() -> None:
    subject = _executor(batch_size=1, max_parallel=2)
    in_flight = 0
    peak = 0

    async def fake_adaptive(group, anchor):  # type: ignore[no-untyped-def]
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return [group[0].key]

    subject._execute_adaptive = fake_adaptive  # type: ignore[method-assign]
    result = cast(Any, await subject.execute(_calls(6), ANCHOR))

    assert peak <= 2, f"exceeded the configured wave bound (peak={peak})"
    assert result == [f"k{i}" for i in range(6)]


@pytest.mark.asyncio
async def test_single_batch_takes_the_direct_path() -> None:
    subject = _executor(batch_size=250, max_parallel=8)
    seen: list[int] = []

    async def fake_adaptive(group, anchor):  # type: ignore[no-untyped-def]
        seen.append(len(group))
        return ["only"]

    subject._execute_adaptive = fake_adaptive  # type: ignore[method-assign]
    assert cast(Any, await subject.execute(_calls(10), ANCHOR)) == ["only"]
    assert seen == [10]


def test_rejects_non_positive_parallelism() -> None:
    with pytest.raises(ValueError):
        _executor(batch_size=1, max_parallel=0)


@pytest.mark.asyncio
async def test_first_batch_failure_cancels_siblings_and_propagates() -> None:
    subject = _executor(batch_size=1, max_parallel=4)
    completed = 0
    cancelled = 0

    async def fake_adaptive(group, anchor):  # type: ignore[no-untyped-def]
        nonlocal completed, cancelled
        if group[0].key == "k3":
            raise RuntimeError("batch 3 failed")
        try:
            for _ in range(3):
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            cancelled += 1
            raise
        completed += 1
        return [group[0].key]

    subject._execute_adaptive = fake_adaptive  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="batch 3 failed"):
        await subject.execute(_calls(40), ANCHOR)

    assert cancelled > 0, "in-flight siblings were not cancelled by the first failure"
    assert completed + cancelled < 40, "batches beyond the failure were still started"


def _verified_batch(keys: Sequence[str]) -> VerifiedBatchResult:
    results = tuple(RawCallResult(key, True, word(int(key[1:]))) for key in keys)
    return VerifiedBatchResult(
        results,
        VerificationEvidence(
            ExecutorKind.MULTICALL3,
            "eip1898",
            ANCHOR.block_hash,
            ("provider",),
            digest_raw_results(results),
            True,
        ),
    )


@pytest.mark.asyncio
async def test_batch_sequence_is_stable_when_batches_finish_out_of_order() -> None:
    subject = _executor(batch_size=2, max_parallel=8)
    finish_order: list[str] = []

    async def fake_adaptive(group, anchor):  # type: ignore[no-untyped-def]
        # Earlier batches finish last so the input order and completion order differ.
        for _ in range(10 - int(group[0].key[1:])):
            await asyncio.sleep(0)
        finish_order.append(group[0].key)
        return [_verified_batch([call.key for call in group])]

    subject._execute_adaptive = fake_adaptive  # type: ignore[method-assign]
    specs = [UIntCallSpec(call, call.target, "balanceOf") for call in _calls(10)]
    decoded = await execute_uint_calls(subject, specs, ANCHOR)

    assert finish_order == ["k8", "k6", "k4", "k2", "k0"]
    assert [batch.batch_sequence for batch in decoded.batches] == [0, 1, 2, 3, 4]
    assert [decoded.calls[f"k{i}"].batch_sequence for i in range(10)] == [i // 2 for i in range(10)]
    assert [decoded.calls[f"k{i}"].observation.value for i in range(10)] == list(range(10))


# ------------------------------------------------- same-endpoint bounded retry (C2)


_Outcome = BaseException | str | asyncio.Event


def _endpoint(name: str, **overrides: Any) -> RpcEndpoint:
    return RpcEndpoint(name, f"https://{name}.invalid", name, supports_eip1898=True, **overrides)


class _ScriptedRpc:
    """Fake AsyncRpcClient over a real pool with per-endpoint scripted eth_call outcomes.

    An outcome is a hex response, an exception to raise, or an asyncio.Event the call
    awaits forever (a request hanging on the wire); cancellations there are counted.
    """

    def __init__(
        self,
        endpoints: Sequence[RpcEndpoint],
        outcomes: dict[str, list[_Outcome]],
        *,
        max_retries: int = 5,
    ) -> None:
        self.endpoints = {endpoint.name: endpoint for endpoint in endpoints}
        self.endpoint_pool = EndpointPool(endpoints)
        self.max_retries = max_retries
        self.retry_base_seconds = 0.25
        self.outcomes = outcomes
        # (endpoint name, consecutive_failures, cooldown_until) as seen by each call.
        self.calls: list[tuple[str, int, float]] = []
        self.cancelled = 0

    async def call_on_endpoint(self, endpoint: RpcEndpoint, method: str, params: Any) -> Any:
        assert endpoint is self.endpoints[endpoint.name]
        assert method == "eth_call"
        self.calls.append((endpoint.name, endpoint.consecutive_failures, endpoint.cooldown_until))
        outcome = self.outcomes[endpoint.name].pop(0)
        if isinstance(outcome, asyncio.Event):
            try:
                await outcome.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            raise AssertionError("a hanging request must only end by cancellation")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _SingleEndpointRpc(_ScriptedRpc):
    def __init__(self, outcomes: list[_Outcome], *, max_retries: int = 5) -> None:
        self.endpoint = _endpoint("only")
        super().__init__([self.endpoint], {"only": outcomes}, max_retries=max_retries)


def _aggregate3_response(body: Sequence[bytes]) -> str:
    entries = list(sentinels()) + [(True, item) for item in body] + list(sentinels())
    return "0x" + abi_encode(["(bool,bytes)[]"], [entries]).hex()


def _rpc_executor(
    rpc: _ScriptedRpc, *, batch_size: int = 250, max_parallel: int = 8
) -> Multicall3Executor:
    return Multicall3Executor(
        cast(Any, rpc),
        address="0x" + "ca" * 20,
        deployment_block=0,
        batch_size=batch_size,
        max_parallel_batches=max_parallel,
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


@pytest.mark.asyncio
async def test_single_endpoint_rate_limit_retries_same_endpoint_after_retry_after(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    verified: list[str] = []
    original_verify = Multicall3Executor._verify_sentinels

    def counting_verify(results: Any, anchor: BlockRef, position: str) -> None:
        verified.append(position)
        original_verify(results, anchor, position)

    monkeypatch.setattr(Multicall3Executor, "_verify_sentinels", staticmethod(counting_verify))

    rpc = _SingleEndpointRpc(
        [
            RpcHttpError(429, "Too Many Requests", 1.5),
            _aggregate3_response([word(7), word(9)]),
        ]
    )
    (batch,) = await _rpc_executor(rpc).execute(_calls(2), ANCHOR)

    assert [result.returndata for result in batch.results] == [word(7), word(9)]
    assert batch.evidence.verified and batch.evidence.result_digest == digest_raw_results(
        batch.results
    )
    assert sleeps == [1.5], "Retry-After must win over the exponential backoff"
    assert verified == ["head", "tail"], "sentinels must be verified on the retried response"
    # The 429 still escalated the endpoint's cooldown before the same-endpoint retry.
    assert [failures for _, failures, _ in rpc.calls] == [0, 1]
    assert rpc.calls[1][2] > 0.0
    assert rpc.endpoint.consecutive_failures == 0


@pytest.mark.asyncio
async def test_single_endpoint_backoff_wins_over_short_retry_after(sleeps: list[float]) -> None:
    rpc = _SingleEndpointRpc(
        [
            RpcHttpError(503, "Service Unavailable", None),
            RpcHttpError(503, "Service Unavailable", 0.1),
            _aggregate3_response([word(1)]),
        ]
    )
    (batch,) = await _rpc_executor(rpc).execute(_calls(1), ANCHOR)

    assert batch.results[0].returndata == word(1)
    assert sleeps == [0.25, 0.5]


@pytest.mark.asyncio
async def test_single_endpoint_permanent_error_does_not_retry(sleeps: list[float]) -> None:
    rpc = _SingleEndpointRpc([RpcResponseError(-32603, "internal error")])

    with pytest.raises(RpcResponseError, match="-32603"):
        await _rpc_executor(rpc).execute(_calls(1), ANCHOR)

    assert len(rpc.calls) == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_single_endpoint_retries_are_bounded_by_max_retries(
    sleeps: list[float],
) -> None:
    rpc = _SingleEndpointRpc(
        [RpcHttpError(429, "Too Many Requests", 2.0) for _ in range(3)], max_retries=3
    )

    with pytest.raises(RpcNoHealthyEndpoint):
        await _rpc_executor(rpc).execute(_calls(1), ANCHOR)

    assert len(rpc.calls) == 3
    assert sleeps == [2.0, 2.0]


@pytest.mark.asyncio
async def test_sentinel_mismatch_on_the_only_endpoint_fails_closed(sleeps: list[float]) -> None:
    wrong_sentinels = list(sentinels(block=101)) + [(True, word(1))] + list(sentinels())
    rpc = _SingleEndpointRpc(["0x" + abi_encode(["(bool,bytes)[]"], [wrong_sentinels]).hex()] * 2)

    with pytest.raises(RpcNoHealthyEndpoint):
        await _rpc_executor(rpc).execute(_calls(1), ANCHOR)

    assert len(rpc.calls) == 1, "a wrong-state answer is not a retryable class"


@pytest.mark.asyncio
async def test_sentinel_excluded_endpoint_is_never_reselected_after_a_rate_limit(
    sleeps: list[float],
) -> None:
    wrong_sentinels = list(sentinels(block=101)) + [(True, word(1))] + list(sentinels())
    rpc = _ScriptedRpc(
        [_endpoint("a"), _endpoint("b")],
        {
            "a": ["0x" + abi_encode(["(bool,bytes)[]"], [wrong_sentinels]).hex()] * 2,
            "b": [RpcHttpError(429, "Too Many Requests", None) for _ in range(5)],
        },
    )

    with pytest.raises(RpcNoHealthyEndpoint):
        await _rpc_executor(rpc).execute(_calls(1), ANCHOR)

    # a answered with the wrong state and was excluded by the caller; the same-endpoint
    # retry after b's 429s must not bring it back.
    assert [name for name, _, _ in rpc.calls] == ["a"] + ["b"] * 5


# ---------------------------------------------- Retry-After only when reusing (C2/C3)


@pytest.mark.asyncio
async def test_failover_to_a_healthy_endpoint_keeps_the_short_backoff(
    sleeps: list[float],
) -> None:
    rpc = _ScriptedRpc(
        [_endpoint("a"), _endpoint("b")],
        {
            "a": [RpcHttpError(429, "Too Many Requests", 60.0)],
            "b": [_aggregate3_response([word(5)])],
        },
    )
    (batch,) = await _rpc_executor(rpc).execute(_calls(1), ANCHOR)

    assert batch.results[0].returndata == word(5)
    assert [name for name, _, _ in rpc.calls] == ["a", "b"]
    assert sleeps == [0.25], "a's Retry-After must not delay the hop to healthy b"


@pytest.mark.parametrize("reason", ["cooldown", "archive"])
@pytest.mark.asyncio
async def test_same_endpoint_retry_engages_when_the_other_endpoint_cannot_serve(
    sleeps: list[float], reason: str
) -> None:
    a = _endpoint("a")
    b = _endpoint("b", archive_from_block=ANCHOR.number + 1 if reason == "archive" else None)
    rpc = _ScriptedRpc(
        [a, b],
        {
            "a": [
                RpcHttpError(429, "Too Many Requests", 0.1),
                _aggregate3_response([word(3)]),
            ],
            "b": [],
        },
    )
    if reason == "cooldown":
        # A sibling batch's 429 put b into cooldown.
        rpc.endpoint_pool.record_failure(b, failover=True)

    (batch,) = await _rpc_executor(rpc).execute(_calls(1), ANCHOR)

    assert batch.results[0].returndata == word(3)
    assert [name for name, _, _ in rpc.calls] == ["a", "a"]
    assert sleeps == [0.25], "backoff (0.25) wins over the short Retry-After when reusing a"


# ------------------------------------------ sibling cancellation is not a failure (C3)


@pytest.mark.asyncio
async def test_cancelled_sibling_does_not_escalate_endpoint_cooldown() -> None:
    hanging = asyncio.Event()
    rpc = _SingleEndpointRpc([hanging, RpcHttpError(400, "Bad Request")])
    subject = _rpc_executor(rpc, batch_size=1, max_parallel=2)

    with pytest.raises(RpcHttpError, match="400"):
        await subject.execute(_calls(2), ANCHOR)

    assert rpc.cancelled == 1, "the hanging batch was not cancelled"
    assert rpc.endpoint.consecutive_failures == 1, "cancellation counted as a failure"
    assert rpc.endpoint.healthy


@pytest.mark.asyncio
async def test_cancellation_while_selecting_an_endpoint_stays_a_cancellation() -> None:
    rpc = _SingleEndpointRpc([])
    subject = _rpc_executor(rpc)
    blocked = asyncio.Event()

    async def hanging_select(anchor: BlockRef, excluded: set[str]) -> RpcEndpoint:
        await blocked.wait()
        raise AssertionError("unreachable")

    subject._select = hanging_select  # type: ignore[method-assign]
    task = asyncio.create_task(subject._eth_call(b"\x00" * 4, ANCHOR))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert rpc.calls == []
    assert rpc.endpoint.consecutive_failures == 0
