"""Health-aware endpoint selection and distinct-provider quorum selection."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable

from .endpoint import RpcEndpoint
from .errors import RpcNoHealthyEndpoint


class EndpointPool:
    def __init__(self, endpoints: Iterable[RpcEndpoint]) -> None:
        self._endpoints = tuple(endpoints)
        if not self._endpoints:
            raise ValueError("at least one RPC endpoint is required")
        names = [endpoint.name for endpoint in self._endpoints]
        if len(names) != len(set(names)):
            raise ValueError("RPC endpoint names must be unique")
        self._cursor = 0
        self._lock = asyncio.Lock()

    @property
    def endpoints(self) -> tuple[RpcEndpoint, ...]:
        return self._endpoints

    async def select(
        self,
        *,
        historical_block: int | None = None,
        require_eip1898: bool = False,
        exclude: frozenset[str] = frozenset(),
    ) -> RpcEndpoint:
        async with self._lock:
            count = len(self._endpoints)
            for offset in range(count):
                index = (self._cursor + offset) % count
                endpoint = self._endpoints[index]
                if endpoint.name in exclude:
                    continue
                if require_eip1898 and not endpoint.supports_eip1898:
                    continue
                if not endpoint.can_serve(historical_block):
                    continue
                self._cursor = (index + 1) % count
                return endpoint
        raise RpcNoHealthyEndpoint("no RPC endpoint can serve the request")

    def select_distinct_groups(
        self,
        count: int,
        *,
        historical_block: int | None = None,
    ) -> tuple[RpcEndpoint, ...]:
        if count < 1:
            raise ValueError("quorum count must be positive")
        selected: list[RpcEndpoint] = []
        groups: set[str] = set()
        for endpoint in self._endpoints:
            if endpoint.provider_group in groups:
                continue
            if not endpoint.can_serve(historical_block):
                continue
            selected.append(endpoint)
            groups.add(endpoint.provider_group)
            if len(selected) == count:
                return tuple(selected)
        raise RpcNoHealthyEndpoint(
            f"need {count} healthy distinct provider groups; found {len(selected)}"
        )

    def record_success(self, endpoint: RpcEndpoint) -> None:
        endpoint.healthy = True
        endpoint.consecutive_failures = 0
        endpoint.cooldown_until = 0.0

    def record_failure(
        self,
        endpoint: RpcEndpoint,
        *,
        failover: bool,
        historical_block: int | None = None,
        archive_unavailable: bool = False,
    ) -> None:
        endpoint.consecutive_failures += 1
        if archive_unavailable and historical_block is not None:
            previous = endpoint.archive_unavailable_through
            endpoint.archive_unavailable_through = max(previous or 0, historical_block)
        if failover:
            endpoint.healthy = False
            delay = min(60.0, float(2 ** min(endpoint.consecutive_failures, 6)))
            endpoint.cooldown_until = time.monotonic() + delay

    async def close(self) -> None:
        await asyncio.gather(*(endpoint.close() for endpoint in self._endpoints))

