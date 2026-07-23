"""Convert low-level failures into retry and endpoint-health decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx

from rpc_state_indexer.errors import ArchiveStateUnavailable, RpcError

from .errors import (
    RpcHttpError,
    RpcProtocolError,
    RpcProviderLimit,
    RpcResponseError,
    RpcTransportError,
)


class FailureKind(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    ARCHIVE_UNAVAILABLE = "archive_unavailable"
    PROVIDER_LIMIT = "provider_limit"
    EXECUTION_REVERT = "execution_revert"
    PERMANENT = "permanent"


@dataclass(frozen=True, slots=True)
class RpcFailure:
    kind: FailureKind
    retryable: bool
    failover: bool
    retry_after: float | None = None


_ARCHIVE_MARKERS = (
    "missing trie node",
    "historical state unavailable",
    "state is not available",
    "state unavailable",
    "pruned",
    "required historical state",
)
_LIMIT_MARKERS = (
    "query returned more than",
    "exceeds max results",  # Reth/Erigon: "query exceeds max results 20000, retry with the range …"
    "too many results",
    "too many logs",
    "max logs per response",
    "response size exceeded",
    "request entity too large",
    "block range is too wide",
    "block range too wide",
    "please limit",
    "limit exceeded",
    "request timed out",
    "out of gas",
    "gas required exceeds",
)
# JSON-RPC error codes some providers use for a getLogs range/result cap regardless of the
# message text. -32602 is deliberately excluded: it is generic "invalid params" and is only a
# range signal when one of the markers above appears in its message/data.
_LIMIT_CODES = frozenset({-32005, -32016})
_REVERT_MARKERS = ("execution reverted", "revert")


def _message(exc: BaseException) -> str:
    # Some providers keep `message` generic ("invalid params") and put the real reason
    # (e.g. "query returned more than N results, retry with …") in the JSON-RPC `data`
    # field. Scan both so a range/result cap is recognised wherever the provider puts it.
    text = str(exc)
    data = getattr(exc, "data", None)
    if data is not None:
        text = f"{text} {data}"
    return text.casefold()


def classify_rpc_failure(exc: BaseException) -> RpcFailure:
    """Classify without turning application errors into transport retries."""

    if isinstance(exc, ArchiveStateUnavailable):
        return RpcFailure(FailureKind.ARCHIVE_UNAVAILABLE, False, True)
    if isinstance(exc, RpcProviderLimit):
        return RpcFailure(FailureKind.PROVIDER_LIMIT, False, False)
    if isinstance(exc, RpcHttpError):
        if exc.status_code == 429:
            return RpcFailure(FailureKind.RATE_LIMIT, True, True, exc.retry_after)
        if exc.status_code in {408, 425, 500, 502, 503, 504}:
            return RpcFailure(FailureKind.TRANSIENT, True, True, exc.retry_after)
        if exc.status_code == 413:
            return RpcFailure(FailureKind.PROVIDER_LIMIT, False, False)
        return RpcFailure(FailureKind.PERMANENT, False, False)
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            RpcTransportError,
            RpcProtocolError,
        ),
    ):
        return RpcFailure(FailureKind.TRANSIENT, True, True)

    message = _message(exc)
    if any(marker in message for marker in _ARCHIVE_MARKERS):
        return RpcFailure(FailureKind.ARCHIVE_UNAVAILABLE, False, True)
    if any(marker in message for marker in _LIMIT_MARKERS):
        return RpcFailure(FailureKind.PROVIDER_LIMIT, False, False)
    if isinstance(exc, RpcResponseError) and exc.code in _LIMIT_CODES:
        return RpcFailure(FailureKind.PROVIDER_LIMIT, False, False)
    if isinstance(exc, RpcResponseError) and any(
        marker in message for marker in _REVERT_MARKERS
    ):
        return RpcFailure(FailureKind.EXECUTION_REVERT, False, False)
    if isinstance(exc, RpcError):
        return RpcFailure(FailureKind.PERMANENT, False, False)
    return RpcFailure(FailureKind.PERMANENT, False, False)


def normalize_rpc_error(exc: BaseException) -> BaseException:
    """Promote recognizable provider messages to stable public exception types."""

    failure = classify_rpc_failure(exc)
    if failure.kind is FailureKind.ARCHIVE_UNAVAILABLE:
        return ArchiveStateUnavailable(str(exc))
    if failure.kind is FailureKind.PROVIDER_LIMIT:
        return RpcProviderLimit(str(exc))
    return exc
