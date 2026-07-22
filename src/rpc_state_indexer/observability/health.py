"""Small, dependency-light HTTP server for liveness, readiness, and Prometheus."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Final, cast

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

ReadinessProbe = Callable[[], bool]
_LIVE_PATH: Final = "/live"
_READY_PATH: Final = "/ready"
_METRICS_PATH: Final = "/metrics"
_HEALTH_PATH: Final = "/health"


class _HealthHandler(BaseHTTPRequestHandler):
    """Serve health checks without logging request paths or headers."""

    readiness_probe: ReadinessProbe = staticmethod(lambda: True)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == _LIVE_PATH:
            self._respond(HTTPStatus.OK, b"live\n", "text/plain; charset=utf-8")
            return
        if self.path == _READY_PATH:
            ready = self._is_ready()
            status = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
            body = b"ready\n" if ready else b"not ready\n"
            self._respond(status, body, "text/plain; charset=utf-8")
            return
        if self.path == _HEALTH_PATH:
            health_status = "ready" if self._is_ready() else "degraded"
            self._respond(
                HTTPStatus.OK,
                json.dumps({"status": health_status}, sort_keys=True).encode() + b"\n",
                "application/json",
            )
            return
        if self.path == _METRICS_PATH:
            self._respond(HTTPStatus.OK, generate_latest(REGISTRY), CONTENT_TYPE_LATEST)
            return
        self._respond(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802
        if self.path == _LIVE_PATH:
            self._respond(HTTPStatus.OK, b"", "text/plain; charset=utf-8")
            return
        if self.path == _READY_PATH:
            status = HTTPStatus.OK if self._is_ready() else HTTPStatus.SERVICE_UNAVAILABLE
            self._respond(status, b"", "text/plain; charset=utf-8")
            return
        if self.path == _HEALTH_PATH:
            self._respond(HTTPStatus.OK, b"", "application/json")
            return
        if self.path == _METRICS_PATH:
            self._respond(HTTPStatus.OK, b"", CONTENT_TYPE_LATEST)
            return
        self._respond(HTTPStatus.NOT_FOUND, b"", "text/plain; charset=utf-8")

    def log_message(self, _format: str, *_args: object) -> None:
        """Keep probe request details out of application logs."""

    def _is_ready(self) -> bool:
        try:
            return bool(self.readiness_probe())
        except Exception:
            return False

    def _respond(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


@dataclass(slots=True)
class HealthServer:
    """Owns the background HTTP server and exposes a deterministic shutdown."""

    _server: ThreadingHTTPServer
    _thread: Thread

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> HealthServer:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def start_health_server(
    port: int,
    *,
    host: str = "0.0.0.0",
    readiness_probe: ReadinessProbe | None = None,
) -> HealthServer:
    """Start the observability listener and return its lifecycle handle.

    The caller owns the readiness predicate: it should remain false until required
    configuration and external dependencies have been validated.
    """

    handler = cast(type[_HealthHandler], type("HealthHandler", (_HealthHandler,), {}))
    handler.readiness_probe = staticmethod(readiness_probe or (lambda: True))
    server = ThreadingHTTPServer((host, port), handler)
    thread = Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    return HealthServer(server, thread)
