"""Async JSON-RPC client with bounded retries and endpoint failover."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from rpc_state_indexer.errors import ArchiveStateUnavailable, RpcAttemptsExhausted
from rpc_state_indexer.rpc.classification import (
    FailureKind,
    classify_rpc_failure,
    normalize_rpc_error,
)
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.rpc.endpoint_pool import EndpointPool
from rpc_state_indexer.rpc.errors import (
    RpcHttpError,
    RpcProtocolError,
    RpcResponseError,
    RpcTransportError,
)

JsonValue = Any


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, min(60.0, float(raw)))
    except ValueError:
        return None


class AsyncRpcClient:
    def __init__(
        self,
        endpoint_pool: EndpointPool,
        *,
        concurrency: int = 8,
        max_retries: int = 5,
        retry_base_seconds: float = 0.25,
    ) -> None:
        if concurrency < 1 or max_retries < 1:
            raise ValueError("concurrency and max_retries must be positive")
        self.endpoint_pool = endpoint_pool
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self._semaphore = asyncio.Semaphore(concurrency)
        self._id_lock = asyncio.Lock()
        self._request_id = 0

    async def _next_id(self) -> int:
        async with self._id_lock:
            self._request_id += 1
            return self._request_id

    async def _post(self, endpoint: RpcEndpoint, payload: JsonValue) -> JsonValue:
        await endpoint.throttle()
        client = endpoint.client
        if client is None:
            raise RpcTransportError(f"endpoint {endpoint.name} has no HTTP client")
        try:
            async with self._semaphore:
                response = await client.post(endpoint.url, json=payload)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RpcTransportError(f"endpoint {endpoint.name} transport failure") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise RpcHttpError(
                response.status_code,
                response.reason_phrase,
                _retry_after_seconds(response),
            )
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise RpcProtocolError(f"endpoint {endpoint.name} returned invalid JSON") from exc

    async def call_on_endpoint(
        self,
        endpoint: RpcEndpoint,
        method: str,
        params: Sequence[JsonValue],
    ) -> JsonValue:
        request_id = await self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": list(params),
        }
        body = await self._post(endpoint, payload)
        if not isinstance(body, Mapping):
            raise RpcProtocolError("single JSON-RPC response must be an object")
        if body.get("jsonrpc") not in {None, "2.0"}:
            raise RpcProtocolError("unexpected JSON-RPC version")
        if body.get("id") != request_id:
            raise RpcProtocolError("JSON-RPC response ID mismatch")
        error = body.get("error")
        if error is not None:
            if not isinstance(error, Mapping):
                raise RpcProtocolError("JSON-RPC error must be an object")
            code, message = error.get("code"), error.get("message")
            if type(code) is not int or not isinstance(message, str):
                raise RpcProtocolError("malformed JSON-RPC error")
            raise RpcResponseError(code, message, error.get("data"))
        if "result" not in body:
            raise RpcProtocolError("JSON-RPC response has neither result nor error")
        return body["result"]

    async def batch_on_endpoint(
        self,
        endpoint: RpcEndpoint,
        requests: Sequence[Mapping[str, JsonValue]],
    ) -> list[dict[str, JsonValue]]:
        if not requests:
            return []
        body = await self._post(endpoint, list(requests))
        if not isinstance(body, list):
            raise RpcProtocolError("JSON-RPC batch response must be an array")
        if not all(isinstance(item, dict) for item in body):
            raise RpcProtocolError("JSON-RPC batch entries must be objects")
        return body

    async def call(
        self,
        method: str,
        params: Sequence[JsonValue],
        *,
        historical_block: int | None = None,
        require_eip1898: bool = False,
    ) -> tuple[JsonValue, RpcEndpoint]:
        last_error: BaseException | None = None
        excluded: set[str] = set()
        for attempt in range(self.max_retries):
            try:
                endpoint = await self.endpoint_pool.select(
                    historical_block=historical_block,
                    require_eip1898=require_eip1898,
                    exclude=frozenset(excluded),
                )
            except BaseException as exc:
                last_error = exc
                break
            try:
                result = await self.call_on_endpoint(endpoint, method, params)
            except BaseException as exc:
                normalized = normalize_rpc_error(exc)
                failure = classify_rpc_failure(normalized)
                last_error = normalized
                self.endpoint_pool.record_failure(
                    endpoint,
                    failover=failure.failover,
                    historical_block=historical_block,
                    archive_unavailable=failure.kind is FailureKind.ARCHIVE_UNAVAILABLE,
                )
                if failure.failover:
                    excluded.add(endpoint.name)
                if not failure.retryable and not failure.failover:
                    raise normalized from exc
                delay = failure.retry_after
                if delay is None:
                    delay = min(10.0, self.retry_base_seconds * (2**attempt))
                if delay:
                    await asyncio.sleep(delay)
                continue
            self.endpoint_pool.record_success(endpoint)
            return result, endpoint
        if isinstance(last_error, ArchiveStateUnavailable):
            raise last_error
        raise RpcAttemptsExhausted(f"{method} exhausted RPC endpoints") from last_error

    async def close(self) -> None:
        await self.endpoint_pool.close()
