"""Small, explicit ClickHouse client factory.

The rest of the application accepts a clickhouse-connect compatible client.  Keeping
client construction here avoids import-time connections and makes storage code easy to
exercise with a fake client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ClickHouseConnectionSettings:
    host: str
    username: str
    password: str
    database: str
    port: int = 8443
    secure: bool = True
    verify: bool = True
    connect_timeout_seconds: int = 10
    send_receive_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError("ClickHouse host is required")
        if not self.username:
            raise ValueError("ClickHouse username is required")
        if not 1 <= self.port <= 65535:
            raise ValueError("ClickHouse port must be between 1 and 65535")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("connect timeout must be positive")
        if self.send_receive_timeout_seconds <= 0:
            raise ValueError("send/receive timeout must be positive")


def create_clickhouse_client(
    settings: ClickHouseConnectionSettings,
    *,
    connect_to_database: bool = True,
) -> Any:
    """Create a clickhouse-connect client without connecting at import time.

    Migration bootstrap uses ``connect_to_database=False`` because the configured
    database might not exist yet.  All normal repository work connects directly to the
    configured database.
    """

    try:
        import clickhouse_connect  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - packaging error, not business logic
        raise RuntimeError(
            "clickhouse-connect is required for ClickHouse persistence"
        ) from exc

    kwargs: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "username": settings.username,
        "password": settings.password,
        "secure": settings.secure,
        "verify": settings.verify,
        "connect_timeout": settings.connect_timeout_seconds,
        "send_receive_timeout": settings.send_receive_timeout_seconds,
    }
    if connect_to_database:
        kwargs["database"] = settings.database

    return clickhouse_connect.get_client(**kwargs)
