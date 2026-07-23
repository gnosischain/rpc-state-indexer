from __future__ import annotations

from rpc_state_indexer.rpc.classification import (
    FailureKind,
    classify_rpc_failure,
    normalize_rpc_error,
)
from rpc_state_indexer.rpc.errors import RpcProviderLimit, RpcResponseError


def test_reth_exceeds_max_results_is_a_splittable_provider_limit() -> None:
    # Reth/Erigon reject a dense eth_getLogs window with this exact phrasing. It must classify
    # as PROVIDER_LIMIT and normalize to RpcProviderLimit so the discovery scanner subdivides it
    # instead of failing the whole range closed.
    exc = RpcResponseError(
        -32602, "query exceeds max results 20000, retry with the range 100-200"
    )
    assert classify_rpc_failure(exc).kind is FailureKind.PROVIDER_LIMIT
    assert isinstance(normalize_rpc_error(exc), RpcProviderLimit)


def test_reason_hidden_in_data_field_is_still_recognised() -> None:
    # Some providers keep `message` generic and put the real reason in `data`.
    exc = RpcResponseError(
        -32602, "invalid params", "Query returned more than 50000 results"
    )
    assert classify_rpc_failure(exc).kind is FailureKind.PROVIDER_LIMIT


def test_dedicated_limit_error_code_without_marker_is_provider_limit() -> None:
    exc = RpcResponseError(-32005, "capacity reached")
    assert classify_rpc_failure(exc).kind is FailureKind.PROVIDER_LIMIT


def test_plain_invalid_params_is_not_treated_as_a_range_error() -> None:
    # -32602 with no range/result marker is a genuine bad request, not a splittable limit.
    exc = RpcResponseError(-32602, "invalid params")
    assert classify_rpc_failure(exc).kind is not FailureKind.PROVIDER_LIMIT
    assert not isinstance(normalize_rpc_error(exc), RpcProviderLimit)
