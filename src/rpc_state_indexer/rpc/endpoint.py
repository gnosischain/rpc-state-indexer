"""Endpoint state without leaking endpoint URLs into logs or metrics."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


class AsyncRateGate:
    """Small monotonic per-endpoint rate gate.

    The global concurrency semaphore lives in :class:`AsyncRpcClient`; this gate
    only spaces request starts and deliberately has no background task.
    """

    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._interval = 1.0 / requests_per_second
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            if delay:
                await asyncio.sleep(delay)
            self._next_at = max(now, self._next_at) + self._interval


@dataclass(slots=True)
class RpcEndpoint:
    name: str
    url: str = field(repr=False)
    provider_group: str
    requests_per_second: float = 30.0
    supports_http_batch: bool = True
    supports_eip1898: bool = False
    archive_from_block: int | None = None
    client: httpx.AsyncClient | None = field(default=None, repr=False)

    healthy: bool = True
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    archive_unavailable_through: int | None = None
    _rate_gate: AsyncRateGate = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rate_gate = AsyncRateGate(self.requests_per_second)
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    @property
    def fingerprint(self) -> str:
        parsed = urlsplit(self.url)
        sanitized = f"{parsed.scheme}://{parsed.hostname or ''}:{parsed.port or ''}"
        return hashlib.sha256(sanitized.encode()).hexdigest()

    def can_serve(self, historical_block: int | None = None) -> bool:
        if not self.healthy and time.monotonic() < self.cooldown_until:
            return False
        if historical_block is None:
            return True
        if self.archive_from_block is not None and historical_block < self.archive_from_block:
            return False
        return not (
            self.archive_unavailable_through is not None
            and historical_block <= self.archive_unavailable_through
        )

    async def throttle(self) -> None:
        await self._rate_gate.wait()

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
