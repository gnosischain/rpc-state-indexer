"""Layer 2 — deterministic, RPC-free computation over published primitives.

A ``ComputeModule`` reads only ``v_*_published`` views and writes a derived table, carrying
provenance (``source_attempt_id`` + ``source_result_digest``) back to the verified
publication. It never touches the RPC path, the anchor resolver, or the writer heartbeat —
its "verification" is recomputability: re-running a date reproduces byte-identical data rows,
and each row traces to the exact verified snapshot it was derived from.

Plug-in surface: a new metric is a new module here plus its output-table migration and a
registry entry. Ingestion is untouched. See [[indexer-two-layer-architecture]].
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ComputeStore(Protocol):
    """The read/write surface a compute module needs (satisfied by ClickHouseRepository)."""

    database: str

    def query_rows(
        self, sql: str, parameters: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]: ...

    def insert_rows(
        self,
        table: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        deduplication_token: str | None = None,
    ) -> int: ...


@runtime_checkable
class ComputeModule(Protocol):
    name: str
    output_table: str

    def sources(self) -> tuple[str, ...]:
        """The ``v_*_published`` views this module reads (documentation + wiring check)."""
        ...

    def compute(
        self, store: ComputeStore, *, chain_id: int, snapshot_date: date
    ) -> int:
        """Recompute the derived rows for one date; return the number written."""
        ...
