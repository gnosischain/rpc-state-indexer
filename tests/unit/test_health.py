from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from rpc_state_indexer.observability.health import HealthServer, start_health_server


@contextmanager
def health_server(*, ready: bool = True) -> Iterator[HealthServer]:
    with start_health_server(0, host="127.0.0.1", readiness_probe=lambda: ready) as server:
        yield server


def get(server: HealthServer, path: str) -> tuple[int, str]:
    with urlopen(f"http://127.0.0.1:{server.port}{path}", timeout=2) as response:
        return response.status, response.read().decode("utf-8")


def test_live_and_ready_endpoints_are_successful() -> None:
    with health_server() as server:
        assert get(server, "/live") == (200, "live\n")
        assert get(server, "/ready") == (200, "ready\n")


def test_ready_is_unavailable_when_dependency_probe_fails() -> None:
    with health_server(ready=False) as server:
        assert get(server, "/health") == (200, '{"status": "degraded"}\n')
        with pytest.raises(HTTPError) as raised:
            get(server, "/ready")

    assert raised.value.code == 503


def test_ready_is_unavailable_when_dependency_probe_raises() -> None:
    def broken_probe() -> bool:
        raise RuntimeError("dependency unavailable")

    with start_health_server(0, host="127.0.0.1", readiness_probe=broken_probe) as server:
        with pytest.raises(HTTPError) as raised:
            get(server, "/ready")

    assert raised.value.code == 503


def test_metrics_are_exposed() -> None:
    with health_server() as server:
        status, body = get(server, "/metrics")

    assert status == 200
    assert "python_info" in body
