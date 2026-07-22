"""Command-line boundary for configuration and operational control.

Imports in this module are deliberately side-effect free.  Network clients are only
constructed inside commands and are always closed before the command exits.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError

from rpc_state_indexer.config.loader import Catalog
from rpc_state_indexer.config.validation import validate_runtime_catalog
from rpc_state_indexer.errors import ConfigError
from rpc_state_indexer.evm.calldata import TOTAL_SUPPLY_SELECTOR
from rpc_state_indexer.rpc.capabilities import (
    EndpointCapabilities,
    probe_endpoint_capabilities,
)
from rpc_state_indexer.rpc.endpoint import RpcEndpoint
from rpc_state_indexer.runtime import (
    build_catalog,
    build_repository,
    build_rpc_runtime,
    clickhouse_connection_settings,
    earliest_archive_probe_token,
)
from rpc_state_indexer.settings import RuntimeSettings
from rpc_state_indexer.storage.clickhouse import create_clickhouse_client
from rpc_state_indexer.storage.migrations import MigrationError, MigrationRunner
from rpc_state_indexer.storage.repositories import ClickHouseRepository

app = typer.Typer(
    name="rpc-state-indexer",
    help="Verified historical EVM state indexer.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

_SERVICE_MODULE = "rpc_state_indexer.service"


def _fail(message: str, *, code: int = 1) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _format_settings_error(exc: ValidationError) -> str:
    """Render Pydantic errors without echoing environment values or secrets."""

    messages: list[str] = []
    for item in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item["loc"])
        messages.append(f"{location}: {item['msg']}")
    return "; ".join(messages)


def _load_settings(**overrides: Any) -> RuntimeSettings:
    clean = {name: value for name, value in overrides.items() if value is not None}
    try:
        return RuntimeSettings(**clean)
    except ValidationError as exc:
        _fail(f"invalid runtime settings: {_format_settings_error(exc)}")


def _load_validated_catalog(settings: RuntimeSettings) -> Catalog:
    catalog = build_catalog(settings)
    validate_runtime_catalog(catalog, settings.abi_root)
    return catalog


def _close_clickhouse(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _query_single_row(
    repository: ClickHouseRepository,
    sql: str,
    chain_id: int,
) -> dict[str, Any]:
    rows = repository.query_rows(sql, {"chain_id": chain_id})
    if len(rows) != 1:
        raise RuntimeError(f"health query returned {len(rows)} rows instead of one")
    return rows[0]


def _status_query(database: str) -> str:
    return f"""
    SELECT
        (
            SELECT count()
            FROM {database}.v_day_anchors_canonical
            WHERE chain_id = {{chain_id:UInt64}}
        ) AS canonical_anchors,
        (
            SELECT max(snapshot_date)
            FROM {database}.v_day_anchors_canonical
            WHERE chain_id = {{chain_id:UInt64}}
        ) AS latest_anchor,
        (
            SELECT count()
            FROM {database}.v_publications_current
            WHERE chain_id = {{chain_id:UInt64}}
        ) AS publications,
        (
            SELECT max(snapshot_date)
            FROM {database}.v_publications_current
            WHERE chain_id = {{chain_id:UInt64}}
        ) AS latest_publication,
        (
            SELECT countIf(coverage_status = 'missing')
            FROM {database}.v_coverage_calendar
            WHERE chain_id = {{chain_id:UInt64}}
        ) AS missing_coverage,
        (
            SELECT count()
            FROM {database}.v_census_attempts_current
            WHERE chain_id = {{chain_id:UInt64}}
              AND status != 'failed'
              AND attempt_id NOT IN
              (
                  SELECT attempt_id
                  FROM {database}.v_publications_current
                  WHERE chain_id = {{chain_id:UInt64}}
              )
        ) AS unfinished_attempts,
        (
            SELECT count()
            FROM {database}.v_census_attempts_current AS a
            WHERE a.chain_id = {{chain_id:UInt64}}
              AND a.status = 'failed'
              AND tuple(
                  a.chain_id,
                  a.job_name,
                  a.target_kind,
                  a.target_address,
                  a.snapshot_date
              ) NOT IN
              (
                  SELECT
                      chain_id,
                      job_name,
                      target_kind,
                      target_address,
                      snapshot_date
                  FROM {database}.v_publications_current
                  WHERE chain_id = {{chain_id:UInt64}}
              )
        ) AS unrepaired_failed_attempts,
        (
            SELECT count()
            FROM {database}.v_census_errors_current AS e
            WHERE e.chain_id = {{chain_id:UInt64}}
              AND tuple(
                  e.chain_id,
                  e.job_name,
                  e.target_kind,
                  e.target_address,
                  e.snapshot_date
              ) NOT IN
              (
                  SELECT
                      chain_id,
                      job_name,
                      target_kind,
                      target_address,
                      snapshot_date
                  FROM {database}.v_publications_current
                  WHERE chain_id = {{chain_id:UInt64}}
              )
        ) AS unresolved_errors
    """


def _validation_query(database: str) -> str:
    return f"""
    SELECT
        (
            SELECT count()
            FROM {database}.v_anchor_conflicts
            WHERE chain_id = {{chain_id:UInt64}}
        ) AS anchor_conflicts,
        (
            SELECT count()
            FROM {database}.v_publication_conflicts
            WHERE chain_id = {{chain_id:UInt64}}
        ) AS publication_conflicts,
        (
            SELECT count()
            FROM {database}.v_census_attempts_current
            WHERE chain_id = {{chain_id:UInt64}}
              AND status != 'failed'
              AND attempt_id NOT IN
              (
                  SELECT attempt_id
                  FROM {database}.v_publications_current
                  WHERE chain_id = {{chain_id:UInt64}}
              )
        ) AS unfinished_attempts,
        (
            SELECT count()
            FROM {database}.v_census_attempts_current AS a
            WHERE a.chain_id = {{chain_id:UInt64}}
              AND a.status = 'failed'
              AND tuple(
                  a.chain_id,
                  a.job_name,
                  a.target_kind,
                  a.target_address,
                  a.snapshot_date
              ) NOT IN
              (
                  SELECT
                      chain_id,
                      job_name,
                      target_kind,
                      target_address,
                      snapshot_date
                  FROM {database}.v_publications_current
                  WHERE chain_id = {{chain_id:UInt64}}
              )
        ) AS unrepaired_failed_attempts,
        (
            SELECT count()
            FROM {database}.v_census_errors_current AS e
            WHERE e.chain_id = {{chain_id:UInt64}}
              AND tuple(
                  e.chain_id,
                  e.job_name,
                  e.target_kind,
                  e.target_address,
                  e.snapshot_date
              ) NOT IN
              (
                  SELECT
                      chain_id,
                      job_name,
                      target_kind,
                      target_address,
                      snapshot_date
                  FROM {database}.v_publications_current
                  WHERE chain_id = {{chain_id:UInt64}}
              )
        ) AS unresolved_errors
    """


def _read_clickhouse_row(
    settings: RuntimeSettings,
    query: Callable[[str], str],
) -> dict[str, Any]:
    repository: ClickHouseRepository | None = None
    try:
        chain_id = build_catalog(settings).chain.chain_id
        repository = build_repository(settings)
        repository.ping()
        return _query_single_row(
            repository,
            query(settings.clickhouse_database),
            chain_id,
        )
    finally:
        if repository is not None:
            _close_clickhouse(repository.client)


def _print_mapping(values: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(dict(values), default=str, sort_keys=True))
        return
    width = max(len(name) for name in values)
    for name, value in values.items():
        typer.echo(f"{name:<{width}}  {value if value is not None else '-'}")


def _parse_date(value: str, option: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(f"{option} must be an ISO date (YYYY-MM-DD)", code=2)


def _parse_optional_date(value: str | None, option: str) -> date | None:
    return None if value is None else _parse_date(value, option)


@app.command("validate-config")
def validate_config(
    chain: Annotated[
        str | None,
        typer.Option("--chain", help="Configured chain name; defaults to CHAIN."),
    ] = None,
    config_root: Annotated[
        Path | None,
        typer.Option("--config-root", help="YAML catalog root; defaults to CONFIG_ROOT."),
    ] = None,
    abi_root: Annotated[
        Path | None,
        typer.Option("--abi-root", help="Committed ABI root; defaults to ABI_ROOT."),
    ] = None,
) -> None:
    """Validate the YAML catalog and referenced ABIs without network access."""

    settings = _load_settings(chain=chain, config_root=config_root, abi_root=abi_root)
    try:
        catalog = _load_validated_catalog(settings)
    except (ConfigError, OSError, ValueError) as exc:
        _fail(f"configuration validation failed: {exc}")
    typer.echo(f"valid: {catalog.summary()}")


@app.command()
def migrate() -> None:
    """Apply immutable, checksum-verified ClickHouse migrations."""

    settings = _load_settings()
    client: Any | None = None
    try:
        connection = clickhouse_connection_settings(settings)
        client = create_clickhouse_client(connection, connect_to_database=False)
        outcomes = MigrationRunner(
            client,
            settings.clickhouse_database,
            settings.migrations_dir,
        ).apply()
    except (MigrationError, ValueError) as exc:
        _fail(f"migration failed: {exc}")
    except Exception as exc:
        _fail(f"migration failed ({type(exc).__name__})")
    finally:
        if client is not None:
            _close_clickhouse(client)

    applied = sum(outcome.status == "applied" for outcome in outcomes)
    skipped = sum(outcome.status == "skipped" for outcome in outcomes)
    typer.echo(
        f"migrations complete: database={settings.clickhouse_database} "
        f"applied={applied} skipped={skipped}"
    )


@app.command()
def status(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit one machine-readable JSON object."),
    ] = False,
) -> None:
    """Show a read-only indexing and publication summary."""

    settings = _load_settings()
    try:
        row = _read_clickhouse_row(settings, _status_query)
    except (ConfigError, OSError, ValueError) as exc:
        _fail(f"status unavailable: {exc}")
    except Exception as exc:
        _fail(f"status unavailable ({type(exc).__name__})")
    _print_mapping(row, json_output=json_output)


@app.command()
def validate(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit one machine-readable JSON object."),
    ] = False,
) -> None:
    """Fail if persisted anchors, publications, attempts, or scans are unhealthy."""

    settings = _load_settings()
    try:
        row = _read_clickhouse_row(settings, _validation_query)
    except (ConfigError, OSError, ValueError) as exc:
        _fail(f"operational validation unavailable: {exc}")
    except Exception as exc:
        _fail(f"operational validation unavailable ({type(exc).__name__})")

    _print_mapping(row, json_output=json_output)
    failing = {name: int(value) for name, value in row.items() if int(value) != 0}
    if failing:
        _fail("operational validation failed: " + ", ".join(sorted(failing)))
    if not json_output:
        typer.echo("operational validation passed")


async def _probe_all(
    settings: RuntimeSettings,
    catalog: Catalog,
) -> list[tuple[RpcEndpoint, EndpointCapabilities | None, str | None]]:
    runtime = build_rpc_runtime(settings, catalog)
    expected_code_hash = catalog.chain.multicall3.runtime_code_hash
    if expected_code_hash is None:  # guarded by validate_runtime_catalog
        raise ConfigError("multicall3.runtime_code_hash is not pinned")
    archive_token = earliest_archive_probe_token(catalog)

    results: list[tuple[RpcEndpoint, EndpointCapabilities | None, str | None]] = []
    try:
        for endpoint in runtime.rpc.endpoint_pool.endpoints:
            try:
                capabilities = await probe_endpoint_capabilities(
                    runtime.rpc,
                    endpoint,
                    expected_chain_id=catalog.chain.chain_id,
                    finality_tag=catalog.chain.finality_tag,
                    multicall_address=catalog.chain.multicall3.address,
                    multicall_deployment_block=(
                        catalog.chain.multicall3.deployment_block
                    ),
                    expected_multicall_code_hash=expected_code_hash,
                    archive_probe_address=archive_token.address,
                    archive_probe_block=archive_token.deployment_block,
                    archive_probe_calldata="0x" + TOTAL_SUPPLY_SELECTOR.hex(),
                )
            except Exception as exc:
                # Keep provider response bodies (and any echoed credentials) out of
                # output and persistence.  The exception class remains actionable.
                results.append((endpoint, None, type(exc).__name__))
            else:
                results.append((endpoint, capabilities, None))
    finally:
        await runtime.close()
    return results


def _capability_row(
    *,
    chain_id: int,
    endpoint: RpcEndpoint,
    capabilities: EndpointCapabilities | None,
    error_class: str | None,
) -> dict[str, Any]:
    if capabilities is None:
        return {
            "chain_id": chain_id,
            "endpoint_fingerprint": endpoint.fingerprint,
            "provider_group": endpoint.provider_group,
            "chain_id_verified": False,
            "supports_http_batch": False,
            "supports_eip1898": False,
            "supports_finality_tag": False,
            "archive_from_block": None,
            "multicall_code_hash_verified": False,
            "healthy": False,
            "error_class": error_class or "ProbeError",
            "error_message": "capability probe failed; endpoint details suppressed",
        }
    return {
        "chain_id": chain_id,
        "endpoint_fingerprint": endpoint.fingerprint,
        "provider_group": endpoint.provider_group,
        "chain_id_verified": capabilities.chain_id_verified,
        "supports_http_batch": capabilities.supports_http_batch,
        "supports_eip1898": capabilities.supports_eip1898,
        "supports_finality_tag": capabilities.supports_finality_tag,
        "archive_from_block": capabilities.archive_from_block,
        "multicall_code_hash_verified": capabilities.multicall_code_hash_verified,
        "healthy": True,
    }


@app.command()
def probe(
    persist: Annotated[
        bool,
        typer.Option(
            "--persist/--no-persist",
            help="Deprecated no-op: probe results are logged, not persisted.",
        ),
    ] = False,
) -> None:
    """Probe RPC safety capabilities without displaying endpoint URLs."""

    del persist  # persistence removed; results are printed below
    settings = _load_settings()
    try:
        catalog = _load_validated_catalog(settings)
        results = asyncio.run(_probe_all(settings, catalog))
    except (ConfigError, OSError, ValueError) as exc:
        _fail(f"RPC probe setup failed: {exc}")
    except Exception as exc:
        _fail(f"RPC probe failed ({type(exc).__name__})")

    rows = [
        _capability_row(
            chain_id=catalog.chain.chain_id,
            endpoint=endpoint,
            capabilities=capabilities,
            error_class=error_class,
        )
        for endpoint, capabilities, error_class in results
    ]

    failed = 0
    for index, row in enumerate(rows, start=1):
        if row["healthy"]:
            typer.echo(
                f"rpc_{index}: ok batch={int(row['supports_http_batch'])} "
                f"eip1898={int(row['supports_eip1898'])} "
                f"finality={int(row['supports_finality_tag'])}"
            )
        else:
            failed += 1
            typer.echo(f"rpc_{index}: failed ({row['error_class']})", err=True)
    if failed:
        _fail(f"{failed}/{len(rows)} endpoint capability probes failed")


def _invoke_service(operation: str, settings: RuntimeSettings, **kwargs: Any) -> None:
    """Load the orchestration layer only when an execution command is requested."""

    try:
        module = importlib.import_module(_SERVICE_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name == _SERVICE_MODULE:
            _fail(
                f"{operation} is not available yet: {_SERVICE_MODULE} has not been wired"
            )
        _fail(f"{operation} could not load its service dependency ({exc.name})")

    entry_name = f"run_{operation}"
    entry = getattr(module, entry_name, None)
    if not callable(entry):
        _fail(f"{operation} is not available yet: missing {entry_name}()")
    try:
        result = entry(settings=settings, **kwargs)
        if inspect.isawaitable(result):
            asyncio.run(_await_result(result))
    except Exception as exc:
        service_error = getattr(module, "ServiceError", None)
        if isinstance(service_error, type) and isinstance(exc, service_error):
            _fail(f"{operation} failed: {exc}")
        _fail(f"{operation} failed ({type(exc).__name__})")


async def _await_result(value: Awaitable[Any]) -> Any:
    return await value


@app.command()
def discover(
    through: Annotated[
        str | None,
        typer.Option("--through", help="Discover through this UTC snapshot date."),
    ] = None,
    job: Annotated[
        str | None,
        typer.Option("--job", help="Limit discovery to one configured job."),
    ] = None,
) -> None:
    """Advance strict event discovery through a snapshot anchor."""

    _invoke_service(
        "discover",
        _load_settings(),
        through=_parse_optional_date(through, "--through"),
        job=job,
    )


@app.command()
def census(
    snapshot_date: Annotated[
        str,
        typer.Option("--date", help="UTC snapshot date to collect."),
    ],
    job: Annotated[
        str | None,
        typer.Option("--job", help="Limit collection to one configured job."),
    ] = None,
) -> None:
    """Collect, verify, and publish one snapshot date."""

    _invoke_service(
        "census",
        _load_settings(),
        snapshot_date=_parse_date(snapshot_date, "--date"),
        job=job,
    )


@app.command()
def compute(
    snapshot_date: Annotated[
        str,
        typer.Option("--date", help="UTC snapshot date to compute derived tables for."),
    ],
    module: Annotated[
        str | None,
        typer.Option("--module", help="Limit to one registered compute module."),
    ] = None,
) -> None:
    """Recompute Layer 2 derived tables from published primitives (RPC-free)."""

    _invoke_service(
        "compute",
        _load_settings(),
        snapshot_date=_parse_date(snapshot_date, "--date"),
        module=module,
    )


@app.command()
def backfill(
    from_date: Annotated[
        str,
        typer.Option("--from", help="First UTC snapshot date, inclusive."),
    ],
    to_date: Annotated[
        str,
        typer.Option("--to", help="Last UTC snapshot date, inclusive."),
    ],
    job: Annotated[
        str | None,
        typer.Option("--job", help="Limit the backfill to one configured job."),
    ] = None,
    daily: Annotated[
        bool,
        typer.Option(
            "--daily/--month-end",
            help="Use daily history instead of the default month-end anchors.",
        ),
    ] = False,
) -> None:
    """Run a bounded historical backfill (month-end anchors by default)."""

    parsed_from = _parse_date(from_date, "--from")
    parsed_to = _parse_date(to_date, "--to")
    if parsed_from > parsed_to:
        _fail("--from must be on or before --to", code=2)
    _invoke_service(
        "backfill",
        _load_settings(),
        from_date=parsed_from,
        to_date=parsed_to,
        job=job,
        daily=daily,
    )


@app.command()
def densify(
    from_date: Annotated[
        str,
        typer.Option("--from", help="First UTC snapshot date, inclusive."),
    ],
    to_date: Annotated[
        str,
        typer.Option("--to", help="Last UTC snapshot date, inclusive."),
    ],
    job: Annotated[
        str | None,
        typer.Option("--job", help="Limit densification to one configured job."),
    ] = None,
) -> None:
    """Fill daily snapshots inside a previously anchored historical range."""

    parsed_from = _parse_date(from_date, "--from")
    parsed_to = _parse_date(to_date, "--to")
    if parsed_from > parsed_to:
        _fail("--from must be on or before --to", code=2)
    _invoke_service(
        "densify",
        _load_settings(),
        from_date=parsed_from,
        to_date=parsed_to,
        job=job,
    )


@app.command()
def bench(
    snapshot_date: Annotated[
        str | None,
        typer.Option("--date", help="Pinned UTC date used by the benchmark."),
    ] = None,
) -> None:
    """Benchmark safe batch ceilings at a pinned historical block."""

    _invoke_service(
        "bench",
        _load_settings(),
        snapshot_date=_parse_optional_date(snapshot_date, "--date"),
    )


@app.command()
def daemon() -> None:
    """Run the continuous per-chain scheduler."""

    _invoke_service("daemon", _load_settings())
