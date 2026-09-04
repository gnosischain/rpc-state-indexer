"""Typed table boundary for ClickHouse persistence.

The repository intentionally exposes append/insert operations.  Repairs create new
attempts; there are no UPDATE, DELETE, mutation, or partition-replacement helpers here.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from .digests import (
    BalanceDigestRow,
    PoolBalanceDigestRow,
    PoolClStateDigestRow,
    PoolTickDigestRow,
    ScalarDigestRow,
    digest_cl_observations,
    digest_pool_observations,
    digest_token_observations,
    digest_universe,
)
from .migrations import validate_database_name

_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class AttemptScope:
    """Sort-key prefix identifying one attempt's rows.

    Every attempt/observation table is ordered by
    ``(chain_id, job_name, <target>_address, snapshot_date, attempt_id, ...)``. A
    read-back that filters on ``attempt_id`` alone cannot use that prefix, so it
    degrades into a full scan (with ``FINAL``) that grows without bound as history
    accumulates. Carrying the prefix keeps every read-back a point lookup.
    """

    chain_id: int
    job_name: str
    target_address: str
    snapshot_date: date
    attempt_id: UUID

    def parameters(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "job_name": self.job_name,
            "target_address": self.target_address,
            "snapshot_date": self.snapshot_date,
            "attempt_id": self.attempt_id,
        }

    @staticmethod
    def predicate(address_column: str) -> str:
        return (
            "chain_id = {chain_id:UInt64}"
            " AND job_name = {job_name:String}"
            f" AND {address_column} = {{target_address:String}}"
            " AND snapshot_date = {snapshot_date:Date}"
            " AND attempt_id = {attempt_id:UUID}"
        )


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "config_registry": (
        "chain_id", "job_name", "target_kind", "target_address", "cadence",
        "integrity_mode", "coverage_start", "coverage_end", "config_hash",
        "canonical_config_json", "enabled", "registered_at",
    ),
    "day_anchors": (
        "chain_id", "snapshot_date", "resolution_id", "block_number", "block_hash",
        "parent_hash", "block_timestamp", "next_block_number", "next_block_hash",
        "next_block_timestamp", "finalized_at_resolution", "resolution_source",
        "endpoint_fingerprint", "resolved_at",
    ),
    "discovery_ranges": (
        "chain_id", "token_address", "topic0", "range_start_block",
        "range_end_block_exclusive", "scan_id", "status", "anchor_block", "anchor_hash",
        "log_count", "holder_count", "attempt_count", "endpoint_fingerprint",
        "error_class", "error_message", "started_at", "heartbeat_at", "finished_at",
    ),
    "holder_universe": (
        "chain_id", "token_address", "holder_address", "source", "source_detail",
        "first_seen_block", "last_seen_block", "observations",
    ),
    "token_metadata": (
        "chain_id", "token_address", "symbol", "name", "decimals",
        "resolution_status", "symbol_encoding", "name_encoding", "anchor_block",
        "anchor_hash", "error_class", "error_message", "observed_at",
    ),
    "sweep_ranges": (
        "chain_id", "wallet_address", "topic_position", "range_start_block",
        "range_end_block_exclusive", "scan_id", "status", "anchor_block", "anchor_hash",
        "log_count", "attempt_count", "endpoint_fingerprint", "error_class",
        "error_message", "started_at", "heartbeat_at", "finished_at",
    ),
    "wallet_interaction_logs": (
        "chain_id", "wallet_address", "topic_position", "contract_address", "topic0",
        "topic_count", "block_number", "block_hash", "transaction_hash", "log_index",
        "topics", "observed_at",
    ),
    "census_attempts": (
        "chain_id", "job_name", "target_kind", "target_address", "snapshot_date",
        "attempt_id", "status", "integrity_mode", "config_hash", "anchor_block",
        "anchor_hash", "executor_kind", "block_reference_kind", "universe_hash",
        "universe_size", "batches_total", "batches_verified", "observations_ok",
        "observations_failed", "result_digest", "batches_json", "error_class",
        "error_message", "started_at", "heartbeat_at", "finished_at",
    ),
    "census_universe_members": (
        "chain_id", "job_name", "target_kind", "target_address", "snapshot_date",
        "attempt_id", "holder_address", "member_sources", "member_ordinal", "inserted_at",
    ),
    "census_errors": (
        "chain_id", "job_name", "target_kind", "target_address", "snapshot_date",
        "attempt_id", "subject_address", "call_kind", "batch_sequence", "error_class",
        "rpc_code", "return_data", "error_message", "terminal_at",
    ),
    "token_balances": (
        "chain_id", "job_name", "token_address", "snapshot_date", "attempt_id",
        "holder_address", "balance_raw", "scaled_balance_raw", "value_kind",
        "probe_source", "batch_sequence", "observed_at",
    ),
    "token_scalars": (
        "chain_id", "job_name", "token_address", "snapshot_date", "attempt_id",
        "scalar_name", "scalar_raw", "probe_source", "batch_sequence", "observed_at",
    ),
    "pool_token_balances": (
        "chain_id", "job_name", "pool_address", "token_address", "snapshot_date",
        "attempt_id", "balance_raw", "probe_source", "batch_sequence", "observed_at",
    ),
    "pool_cl_state": (
        "chain_id", "job_name", "pool_address", "snapshot_date", "attempt_id",
        "pool_class", "sqrt_price_x96", "current_tick", "liquidity",
        "fee_growth_global_0_x128", "fee_growth_global_1_x128", "tick_spacing", "fee",
        "tick_count", "probe_source", "batch_sequence", "observed_at",
    ),
    "pool_tick_liquidity": (
        "chain_id", "job_name", "pool_address", "snapshot_date", "attempt_id",
        "tick", "liquidity_gross", "liquidity_net", "fee_growth_outside_0_x128",
        "fee_growth_outside_1_x128", "probe_source", "batch_sequence", "observed_at",
    ),
    "pool_liquidity_profile": (
        "chain_id", "pool_address", "snapshot_date", "tick_lower", "tick_upper",
        "active_liquidity", "source_attempt_id", "source_result_digest", "computed_at",
    ),
    "census_publications": (
        "chain_id", "job_name", "target_kind", "target_address", "snapshot_date",
        "publication_id", "attempt_id", "executor_kind", "block_reference_kind",
        "integrity_mode", "config_hash", "anchor_block", "anchor_hash", "universe_hash",
        "universe_size", "result_digest", "observed_sum_raw", "reference_supply_raw",
        "batches_total", "observations_total", "provider_groups", "checks_passed",
        "published_at",
    ),
    "writer_heartbeats": (
        "chain_id", "process_id", "operation", "hostname", "details_json", "started_at",
        "heartbeat_at",
    ),
}


class ClickHouseRepository:
    """Append-only persistence over one ClickHouse database.

    clickhouse-connect clients are not safe to share across threads ("use a separate
    client instance per thread/process"). Census bookkeeping runs its blocking
    ClickHouse calls in worker threads so targets can overlap, so each thread lazily
    gets its own client from ``client_factory``; the thread that built the repository
    keeps the original client. Without a factory the repository behaves exactly as
    before: one client, used wherever it is called from.
    """

    def __init__(
        self,
        client: Any,
        database: str,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._owner_client = client
        self._client_factory = client_factory
        self._owner_thread = threading.get_ident()
        self._local = threading.local()
        self._thread_clients: list[Any] = []
        self._thread_clients_lock = threading.Lock()
        self.database = validate_database_name(database)

    @property
    def client(self) -> Any:
        if self._client_factory is None or threading.get_ident() == self._owner_thread:
            return self._owner_client
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._client_factory()
            self._local.client = client
            with self._thread_clients_lock:
                self._thread_clients.append(client)
        return client

    def close_all(self) -> None:
        """Close the owner client and every per-thread client this repository created."""

        with self._thread_clients_lock:
            clients = [self._owner_client, *self._thread_clients]
            self._thread_clients = []
        for client in clients:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def ping(self) -> bool:
        self.client.command("SELECT 1")
        return True

    def insert_rows(
        self,
        table: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        deduplication_token: str | None = None,
    ) -> int:
        """Insert mappings with a deterministic, schema-defined column order."""

        if not _TABLE_RE.fullmatch(table) or table not in TABLE_COLUMNS:
            raise ValueError(f"unsupported persistence table: {table!r}")
        materialized = list(rows)
        if not materialized:
            return 0

        supplied = set(materialized[0])
        allowed = set(TABLE_COLUMNS[table])
        unknown = supplied - allowed
        if unknown:
            raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")
        if "insert_version" in supplied:
            raise ValueError("insert_version is materialized and must not be supplied")

        for index, row in enumerate(materialized[1:], start=1):
            if set(row) != supplied:
                raise ValueError(
                    f"row {index} for {table} has different columns from the first row"
                )

        columns = [column for column in TABLE_COLUMNS[table] if column in supplied]
        data = [[row[column] for column in columns] for row in materialized]
        settings: dict[str, Any] = {"async_insert": 0}
        if deduplication_token is not None:
            if not deduplication_token:
                raise ValueError("deduplication token cannot be empty")
            settings["insert_deduplication_token"] = deduplication_token

        self.client.insert(
            f"{self.database}.{table}",
            data,
            column_names=columns,
            settings=settings,
        )
        return len(materialized)

    def register_configs(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self.insert_rows("config_registry", rows)

    def insert_anchor(self, row: Mapping[str, Any]) -> int:
        return self.insert_rows("day_anchors", [row])

    def insert_discovery_ranges(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self.insert_rows("discovery_ranges", rows)

    def insert_holder_observations(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self.insert_rows("holder_universe", rows)

    def completed_discovery_ranges(
        self,
        chain_id: int,
        token_address: str,
        topic0: str,
    ) -> list[tuple[int, int]]:
        rows = self.query_rows(
            f"""
            SELECT range_start_block, range_end_block_exclusive
            FROM {self.database}.discovery_ranges FINAL
            WHERE chain_id = {{chain_id:UInt64}}
              AND token_address = {{token_address:String}}
              AND topic0 = {{topic0:String}}
              AND status = 'completed'
            ORDER BY range_start_block, range_end_block_exclusive
            """,
            {
                "chain_id": chain_id,
                "token_address": token_address,
                "topic0": topic0,
            },
        )
        return [
            (int(row["range_start_block"]), int(row["range_end_block_exclusive"]))
            for row in rows
        ]

    def insert_token_metadata(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self.insert_rows("token_metadata", rows)

    def unresolved_metadata_addresses(
        self,
        chain_id: int,
        addresses: Iterable[str],
    ) -> tuple[str, ...]:
        """Of ``addresses``, those with no fully-resolved metadata row yet.

        Partial and failed rows are retried: a token can start answering after a proxy
        upgrade, and a transient RPC failure must not permanently label it unknown.
        """

        candidates = tuple(sorted(set(addresses)))
        if not candidates:
            return ()
        rows = self.query_rows(
            f"""
            SELECT token_address
            FROM {self.database}.token_metadata FINAL
            WHERE chain_id = {{chain_id:UInt64}}
              AND resolution_status = 'resolved'
              AND token_address IN {{addresses:Array(String)}}
            """,
            {"chain_id": chain_id, "addresses": list(candidates)},
        )
        resolved = {str(row["token_address"]) for row in rows}
        return tuple(address for address in candidates if address not in resolved)

    def insert_sweep_ranges(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self.insert_rows("sweep_ranges", rows)

    def insert_wallet_interaction_logs(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self.insert_rows("wallet_interaction_logs", rows)

    def completed_sweep_ranges(
        self,
        chain_id: int,
        wallet_address: str,
        topic_position: int,
    ) -> list[tuple[int, int]]:
        rows = self.query_rows(
            f"""
            SELECT range_start_block, range_end_block_exclusive
            FROM {self.database}.sweep_ranges FINAL
            WHERE chain_id = {{chain_id:UInt64}}
              AND wallet_address = {{wallet_address:String}}
              AND topic_position = {{topic_position:UInt8}}
              AND status = 'completed'
            ORDER BY range_start_block, range_end_block_exclusive
            """,
            {
                "chain_id": chain_id,
                "wallet_address": wallet_address,
                "topic_position": topic_position,
            },
        )
        return [
            (int(row["range_start_block"]), int(row["range_end_block_exclusive"]))
            for row in rows
        ]

    def discovered_token_candidates(self, chain_id: int) -> list[tuple[str, int]]:
        """Sweep-discovered fungible candidates as (address, first_seen_block)."""

        rows = self.query_rows(
            f"""
            SELECT
                contract_address,
                min(first_seen_block) AS first_seen_block
            FROM {self.database}.v_sweep_candidate_tokens
            WHERE chain_id = {{chain_id:UInt64}}
              AND token_standard IN ('erc20', 'erc20_weth9')
            GROUP BY contract_address
            ORDER BY contract_address
            """,
            {"chain_id": chain_id},
        )
        return [
            (str(row["contract_address"]), int(row["first_seen_block"])) for row in rows
        ]

    def quarantined_token_targets(
        self,
        chain_id: int,
        job_name: str,
        threshold: int,
    ) -> frozenset[str]:
        """Targets whose last ``threshold`` census attempts for this job all failed."""

        if threshold < 1:
            raise ValueError("quarantine threshold must be positive")
        rows = self.query_rows(
            f"""
            SELECT target_address
            FROM
            (
                SELECT
                    target_address,
                    status,
                    row_number() OVER (
                        PARTITION BY target_address
                        ORDER BY started_at DESC, attempt_id
                    ) AS recency
                FROM {self.database}.census_attempts FINAL
                WHERE chain_id = {{chain_id:UInt64}}
                  AND job_name = {{job_name:String}}
                  AND target_kind = 'token'
            )
            WHERE recency <= {{threshold:UInt32}}
            GROUP BY target_address
            HAVING count() >= {{threshold:UInt32}}
               AND countIf(status != 'failed') = 0
            """,
            {"chain_id": chain_id, "job_name": job_name, "threshold": threshold},
        )
        return frozenset(str(row["target_address"]) for row in rows)

    def holder_addresses(self, chain_id: int, token_address: str) -> tuple[str, ...]:
        rows = self.query_rows(
            f"""
            SELECT holder_address
            FROM {self.database}.holder_universe
            WHERE chain_id = {{chain_id:UInt64}}
              AND token_address = {{token_address:String}}
            GROUP BY holder_address
            HAVING sum(observations) > 0
            ORDER BY holder_address
            """,
            {"chain_id": chain_id, "token_address": token_address},
        )
        return tuple(str(row["holder_address"]) for row in rows)

    def insert_attempt_state(self, row: Mapping[str, Any]) -> int:
        return self.insert_rows("census_attempts", [row])

    def insert_universe_members(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        attempt_id: UUID,
    ) -> int:
        return self.insert_rows(
            "census_universe_members",
            rows,
            deduplication_token=f"universe:{attempt_id}",
        )

    def insert_token_balances(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        attempt_id: UUID,
        batch_sequence: int,
    ) -> int:
        return self.insert_rows(
            "token_balances",
            rows,
            deduplication_token=f"{attempt_id}:balances:{batch_sequence}",
        )

    def insert_token_scalars(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        attempt_id: UUID,
        batch_sequence: int,
    ) -> int:
        return self.insert_rows(
            "token_scalars",
            rows,
            deduplication_token=f"{attempt_id}:scalars:{batch_sequence}",
        )

    def insert_pool_balances(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        attempt_id: UUID,
        batch_sequence: int,
    ) -> int:
        return self.insert_rows(
            "pool_token_balances",
            rows,
            deduplication_token=f"{attempt_id}:pools:{batch_sequence}",
        )

    def insert_pool_cl_state(
        self, rows: Iterable[Mapping[str, Any]], *, attempt_id: UUID
    ) -> int:
        return self.insert_rows(
            "pool_cl_state",
            rows,
            deduplication_token=f"{attempt_id}:cl_state",
        )

    def insert_pool_ticks(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        attempt_id: UUID,
        batch_sequence: int,
    ) -> int:
        return self.insert_rows(
            "pool_tick_liquidity",
            rows,
            deduplication_token=f"{attempt_id}:cl_ticks:{batch_sequence}",
        )

    def insert_terminal_errors(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self.insert_rows("census_errors", rows)

    def append_publication(self, row: Mapping[str, Any]) -> int:
        return self.insert_rows("census_publications", [row])

    def published_target_addresses(
        self,
        *,
        chain_id: int,
        job_name: str,
        target_kind: str,
        snapshot_date: date,
    ) -> frozenset[str]:
        """Every target already published for one (job, kind, date), lowercased.

        One query per job instead of one ``publication_exists`` per target: that
        point lookup is routed through three view layers (a GROUP BY with ten argMax
        and a registry JOIN) and measured at ~185 ms — asked ~3,400 times per date it
        was the single largest line of a census run.
        """

        rows = self._readback_rows(
            f"""
            SELECT lower(target_address) AS target_address
            FROM {self.database}.v_publications_current
            WHERE chain_id = {{chain_id:UInt64}}
              AND job_name = {{job_name:String}}
              AND target_kind = {{target_kind:String}}
              AND snapshot_date = {{snapshot_date:Date}}
            """,
            {
                "chain_id": chain_id,
                "job_name": job_name,
                "target_kind": target_kind,
                "snapshot_date": snapshot_date,
            },
        )
        return frozenset(str(row["target_address"]) for row in rows)

    def publication_exists(
        self,
        *,
        chain_id: int,
        job_name: str,
        target_kind: str,
        target_address: str,
        snapshot_date: date,
    ) -> bool:
        rows = self.query_rows(
            f"""
            SELECT count() AS publications
            FROM {self.database}.v_publications_current
            WHERE chain_id = {{chain_id:UInt64}}
              AND job_name = {{job_name:String}}
              AND target_kind = {{target_kind:String}}
              AND target_address = {{target_address:String}}
              AND snapshot_date = {{snapshot_date:Date}}
            """,
            {
                "chain_id": chain_id,
                "job_name": job_name,
                "target_kind": target_kind,
                "target_address": target_address,
                "snapshot_date": snapshot_date,
            },
        )
        return bool(rows and int(rows[0]["publications"]) > 0)

    def heartbeat(self, row: Mapping[str, Any]) -> int:
        return self.insert_rows("writer_heartbeats", [row])

    def fresh_writer_processes(
        self,
        chain_id: int,
        stale_seconds: int,
        *,
        discovery_scope: bool = False,
    ) -> tuple[str, ...]:
        # Two lock classes share the heartbeat table: `discover` writes only
        # discovery-side tables and conflicts only with other discovery writers;
        # every other operation writes census-side tables and conflicts with each
        # other. A row's class derives from its recorded operation.
        rows = self.query_rows(
            f"""
            SELECT toString(process_id) AS process_id
            FROM {self.database}.writer_heartbeats FINAL
            WHERE chain_id = {{chain_id:UInt64}}
              AND (operation = 'discover') = {{discovery_scope:UInt8}}
              AND heartbeat_at >= now64(9)
                  - toIntervalSecond({{stale_seconds:UInt64}})
            ORDER BY process_id
            """,
            {
                "chain_id": chain_id,
                "stale_seconds": stale_seconds,
                "discovery_scope": int(discovery_scope),
            },
        )
        return tuple(str(row["process_id"]) for row in rows)

    # Read-backs verify rows this process inserted moments ago, possibly through a
    # different per-thread client and therefore a different replica. ClickHouse Cloud
    # replicates asynchronously, so a plain SELECT can miss a just-committed insert and
    # the digest check blocks a perfectly good publication (~43% of targets on a
    # 2-replica service once targets ran concurrently). On SharedMergeTree this setting
    # makes the read wait for every committed insert — the documented read-after-write
    # guarantee — at a few milliseconds' cost on a point lookup.
    _READBACK_SETTINGS: Mapping[str, Any] = {"select_sequential_consistency": 1}

    def query_rows(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        settings: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"parameters": dict(parameters or {})}
        if settings:
            kwargs["settings"] = dict(settings)
        result = self.client.query(sql, **kwargs)
        if hasattr(result, "named_results"):
            return list(result.named_results())
        column_names: Sequence[str] = getattr(result, "column_names", ())
        return [
            dict(zip(column_names, values, strict=True))
            for values in getattr(result, "result_rows", ())
        ]

    def _readback_rows(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return self.query_rows(sql, parameters, settings=self._READBACK_SETTINGS)

    def canonical_anchor(self, chain_id: int, snapshot_date: date) -> dict[str, Any] | None:
        rows = self.query_rows(
            f"""
            SELECT *
            FROM {self.database}.v_day_anchors_canonical
            WHERE chain_id = {{chain_id:UInt64}}
              AND snapshot_date = {{snapshot_date:Date}}
            LIMIT 1
            """,
            {"chain_id": chain_id, "snapshot_date": snapshot_date},
        )
        return rows[0] if rows else None

    def active_config_hash(
        self,
        chain_id: int,
        job_name: str,
        target_kind: str,
        target_address: str,
    ) -> str | None:
        rows = self.query_rows(
            f"""
            SELECT config_hash
            FROM {self.database}.v_config_registry_current
            WHERE chain_id = {{chain_id:UInt64}}
              AND job_name = {{job_name:String}}
              AND target_kind = {{target_kind:String}}
              AND target_address = {{target_address:String}}
            LIMIT 1
            """,
            {
                "chain_id": chain_id,
                "job_name": job_name,
                "target_kind": target_kind,
                "target_address": target_address,
            },
        )
        if not rows:
            return None
        value = rows[0]["config_hash"]
        return value.decode("ascii") if isinstance(value, bytes) else str(value)

    def terminal_error_count(self, scope: AttemptScope) -> int:
        result = self.client.query(
            f"""
            SELECT count()
            FROM {self.database}.census_errors FINAL
            WHERE {AttemptScope.predicate("target_address")}
            """,
            parameters=scope.parameters(),
            settings=dict(self._READBACK_SETTINGS),
        )
        return int(result.result_rows[0][0])

    def readback_universe_digest(self, scope: AttemptScope) -> str:
        rows = self._readback_rows(
            f"""
            SELECT holder_address, member_sources
            FROM {self.database}.census_universe_members FINAL
            WHERE {AttemptScope.predicate("target_address")}
            ORDER BY holder_address
            """,
            scope.parameters(),
        )
        return digest_universe(
            (row["holder_address"], row["member_sources"]) for row in rows
        )

    def readback_token_digest(self, scope: AttemptScope) -> str:
        balances = self._readback_rows(
            f"""
            SELECT holder_address, balance_raw, scaled_balance_raw, value_kind
            FROM {self.database}.token_balances FINAL
            WHERE {AttemptScope.predicate("token_address")}
            ORDER BY holder_address
            """,
            scope.parameters(),
        )
        scalars = self._readback_rows(
            f"""
            SELECT scalar_name, scalar_raw
            FROM {self.database}.token_scalars FINAL
            WHERE {AttemptScope.predicate("token_address")}
            ORDER BY scalar_name
            """,
            scope.parameters(),
        )
        return digest_token_observations(
            (
                BalanceDigestRow(
                    holder_address=str(row["holder_address"]),
                    balance_raw=int(row["balance_raw"]),
                    scaled_balance_raw=(
                        None
                        if row["scaled_balance_raw"] is None
                        else int(row["scaled_balance_raw"])
                    ),
                    value_kind=str(row["value_kind"]),
                )
                for row in balances
            ),
            (
                ScalarDigestRow(
                    scalar_name=str(row["scalar_name"]),
                    scalar_raw=int(row["scalar_raw"]),
                )
                for row in scalars
            ),
        )

    def readback_pool_digest(self, scope: AttemptScope) -> str:
        rows = self._readback_rows(
            f"""
            SELECT pool_address, token_address, balance_raw
            FROM {self.database}.pool_token_balances FINAL
            WHERE {AttemptScope.predicate("pool_address")}
            ORDER BY pool_address, token_address
            """,
            scope.parameters(),
        )
        return digest_pool_observations(
            PoolBalanceDigestRow(
                pool_address=str(row["pool_address"]),
                token_address=str(row["token_address"]),
                balance_raw=int(row["balance_raw"]),
            )
            for row in rows
        )

    def readback_cl_digest(self, scope: AttemptScope) -> str:
        state_rows = self._readback_rows(
            f"""
            SELECT pool_address, sqrt_price_x96, current_tick, liquidity,
                   fee_growth_global_0_x128, fee_growth_global_1_x128,
                   tick_spacing, fee, tick_count
            FROM {self.database}.pool_cl_state FINAL
            WHERE {AttemptScope.predicate("pool_address")}
            LIMIT 1
            """,
            scope.parameters(),
        )
        if not state_rows:
            raise ValueError(f"no persisted CL state for attempt {scope.attempt_id}")
        row = state_rows[0]
        state = PoolClStateDigestRow(
            pool_address=str(row["pool_address"]),
            sqrt_price_x96=int(row["sqrt_price_x96"]),
            current_tick=int(row["current_tick"]),
            liquidity=int(row["liquidity"]),
            fee_growth_global_0_x128=int(row["fee_growth_global_0_x128"]),
            fee_growth_global_1_x128=int(row["fee_growth_global_1_x128"]),
            tick_spacing=int(row["tick_spacing"]),
            fee=int(row["fee"]),
            tick_count=int(row["tick_count"]),
        )
        tick_rows = self._readback_rows(
            f"""
            SELECT pool_address, tick, liquidity_gross, liquidity_net,
                   fee_growth_outside_0_x128, fee_growth_outside_1_x128
            FROM {self.database}.pool_tick_liquidity FINAL
            WHERE {AttemptScope.predicate("pool_address")}
            ORDER BY tick
            """,
            scope.parameters(),
        )
        return digest_cl_observations(
            state,
            (
                PoolTickDigestRow(
                    pool_address=str(tick["pool_address"]),
                    tick=int(tick["tick"]),
                    liquidity_gross=int(tick["liquidity_gross"]),
                    liquidity_net=int(tick["liquidity_net"]),
                    fee_growth_outside_0_x128=int(tick["fee_growth_outside_0_x128"]),
                    fee_growth_outside_1_x128=int(tick["fee_growth_outside_1_x128"]),
                )
                for tick in tick_rows
            ),
        )
