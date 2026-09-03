"""A per-target census failure must be both logged and persisted.

Before this, the census loop only incremented a counter and run_backfill emitted a
bare count, so a deterministically failing target left no trace in the logs or the
warehouse and could not be diagnosed at all.
"""

import json
from datetime import date
from typing import Any, cast

from rpc_state_indexer.service import IndexerService
from rpc_state_indexer.settings import RuntimeSettings


class RecordingRepository:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[dict[str, Any]] = []
        self.fail = fail

    def insert_terminal_errors(self, rows: list[dict[str, Any]]) -> int:
        if self.fail:
            raise RuntimeError("clickhouse unavailable")
        self.rows.extend(rows)
        return len(rows)


class FakeChain:
    chain_id = 100


class FakeCatalog:
    chain = FakeChain()


def _service(repository: Any, catalog: Any) -> IndexerService:
    subject = IndexerService(RuntimeSettings(), "census")
    subject.repository = cast(Any, repository)
    subject.catalog = cast(Any, catalog)
    return subject


def _record(subject: IndexerService, exc: BaseException) -> None:
    subject._record_target_failure(
        job_name="daily_curated_balances",
        target_kind="token",
        target_address="0x" + "ab" * 20,
        target_label="WXDAI",
        snapshot_date=date(2026, 8, 31),
        exc=exc,
    )


def test_failure_is_persisted_with_the_exception_class(capsys: Any) -> None:
    repository = RecordingRepository()
    _record(_service(repository, FakeCatalog()), ValueError("holder universe empty"))

    assert len(repository.rows) == 1
    row = repository.rows[0]
    assert row["chain_id"] == 100
    assert row["job_name"] == "daily_curated_balances"
    assert row["target_kind"] == "token"
    assert row["call_kind"] == "target"
    assert row["error_class"] == "ValueError"
    assert "holder universe empty" in row["error_message"]

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert events[0]["event"] == "census_target_failed"
    assert events[0]["error"] == "ValueError"
    assert events[0]["target"] == "WXDAI"


def test_recording_failure_never_masks_the_original(capsys: Any) -> None:
    # A broken ClickHouse must not turn a target failure into a crash that
    # aborts the remaining targets.
    repository = RecordingRepository(fail=True)
    _record(_service(repository, FakeCatalog()), ValueError("boom"))

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert [e["event"] for e in events] == [
        "census_target_failed",
        "census_target_failure_unrecorded",
    ]


def test_no_repository_or_catalog_is_still_logged(capsys: Any) -> None:
    _record(_service(None, None), ValueError("boom"))

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert [e["event"] for e in events] == ["census_target_failed"]
