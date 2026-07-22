"""Typed JSON-RPC transport and protocol failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rpc_state_indexer.errors import ArchiveStateUnavailable, RpcAttemptsExhausted, RpcError


class RpcTransportError(RpcError):
    """The request did not reach a valid JSON-RPC response."""


class RpcProtocolError(RpcError):
    """The server response violated JSON-RPC framing."""


@dataclass(eq=False)
class RpcHttpError(RpcTransportError):
    status_code: int
    reason: str
    retry_after: float | None = None

    def __str__(self) -> str:
        return f"JSON-RPC HTTP {self.status_code}: {self.reason}"


@dataclass(eq=False)
class RpcResponseError(RpcError):
    code: int
    message: str
    data: Any = None

    def __str__(self) -> str:
        return f"JSON-RPC error {self.code}: {self.message}"


class RpcRateLimited(RpcHttpError):
    """The endpoint rejected work because of a rate limit."""


class RpcProviderLimit(RpcError):
    """The request exceeded an endpoint gas, range, or response-size limit."""


class RpcNoHealthyEndpoint(RpcAttemptsExhausted):
    """No configured endpoint can currently serve the request."""


__all__ = [
    "ArchiveStateUnavailable",
    "RpcAttemptsExhausted",
    "RpcError",
    "RpcHttpError",
    "RpcNoHealthyEndpoint",
    "RpcProtocolError",
    "RpcProviderLimit",
    "RpcRateLimited",
    "RpcResponseError",
    "RpcTransportError",
]
