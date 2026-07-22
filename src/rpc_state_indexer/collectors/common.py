"""Strict uint256 execution and materialization shared by collectors."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rpc_state_indexer.domain import BlockRef, ObservationStatus, UIntObservation
from rpc_state_indexer.evm.decoding import decode_uint256
from rpc_state_indexer.execution.base import (
    ContractCall,
    HistoricalCallExecutor,
    RawCallResult,
    digest_raw_results,
)
from rpc_state_indexer.rpc.classification import FailureKind, classify_rpc_failure
from rpc_state_indexer.rpc.errors import RpcResponseError

from .models import CollectionBatchEvidence, CollectionError


class CollectionProtocolError(RuntimeError):
    """A verified executor violated its all-results/exact-anchor contract."""


@dataclass(frozen=True, slots=True)
class UIntCallSpec:
    call: ContractCall
    subject_address: str
    call_kind: str


@dataclass(frozen=True, slots=True)
class DecodedUIntCall:
    spec: UIntCallSpec
    observation: UIntObservation
    batch_sequence: int
    rpc_code: int | None = None
    rpc_message: str | None = None

    def as_error(self) -> CollectionError:
        if self.observation.ok:
            raise ValueError("successful call cannot be materialized as an error")
        return CollectionError(
            subject_address=self.spec.subject_address,
            call_kind=self.spec.call_kind,
            status=self.observation.status,
            batch_sequence=self.batch_sequence,
            message=(
                self.observation.detail
                or self.rpc_message
                or self.observation.status.value
            ),
            rpc_code=self.rpc_code,
            return_data=self.observation.raw,
        )


@dataclass(frozen=True, slots=True)
class DecodedExecution:
    calls: dict[str, DecodedUIntCall]
    batches: tuple[CollectionBatchEvidence, ...]


def _decode_raw(spec: UIntCallSpec, raw: RawCallResult, sequence: int) -> DecodedUIntCall:
    if raw.success:
        observation = decode_uint256(True, raw.returndata)
    else:
        failure = classify_rpc_failure(
            RpcResponseError(raw.error_code or -1, raw.error_message or "call failed")
        )
        status = (
            ObservationStatus.REVERTED
            if failure.kind is FailureKind.EXECUTION_REVERT
            else ObservationStatus.RPC_ERROR
        )
        observation = UIntObservation(
            status,
            None,
            raw.returndata,
            raw.error_message or "contract call failed",
        )
    return DecodedUIntCall(
        spec=spec,
        observation=observation,
        batch_sequence=sequence,
        rpc_code=raw.error_code,
        rpc_message=raw.error_message,
    )


async def execute_uint_calls(
    executor: HistoricalCallExecutor,
    specs: Sequence[UIntCallSpec],
    anchor: BlockRef,
) -> DecodedExecution:
    """Execute and decode all specs, rejecting structural partial results."""

    expected: dict[str, UIntCallSpec] = {}
    for spec in specs:
        if spec.call.key in expected:
            raise ValueError(f"duplicate collector call key: {spec.call.key}")
        expected[spec.call.key] = spec

    verified_batches = await executor.execute(
        [spec.call for spec in specs],
        anchor,
    )
    decoded: dict[str, DecodedUIntCall] = {}
    evidence: list[CollectionBatchEvidence] = []
    for sequence, batch in enumerate(verified_batches):
        if not batch.evidence.verified:
            raise CollectionProtocolError(f"batch {sequence} is not verified")
        if batch.evidence.anchor_hash.lower() != anchor.block_hash.lower():
            raise CollectionProtocolError(f"batch {sequence} anchor hash mismatch")
        if batch.evidence.result_digest != digest_raw_results(batch.results):
            raise CollectionProtocolError(f"batch {sequence} result digest mismatch")
        evidence.append(
            CollectionBatchEvidence(
                batch_sequence=sequence,
                body_call_count=len(batch.results),
                evidence=batch.evidence,
            )
        )
        for raw in batch.results:
            try:
                spec = expected[raw.key]
            except KeyError as exc:
                raise CollectionProtocolError(
                    f"batch {sequence} returned unexpected key {raw.key!r}"
                ) from exc
            if raw.key in decoded:
                raise CollectionProtocolError(
                    f"collector received duplicate result for {raw.key!r}"
                )
            decoded[raw.key] = _decode_raw(spec, raw, sequence)

    missing = sorted(set(expected) - set(decoded))
    if missing:
        raise CollectionProtocolError(f"collector results are missing keys: {missing}")
    return DecodedExecution(decoded, tuple(evidence))


def completeness_check(
    *,
    expected_calls: int,
    decoded: DecodedExecution,
) -> tuple[bool, int]:
    successful = sum(call.observation.ok for call in decoded.calls.values())
    return successful == expected_calls, successful


__all__ = [
    "CollectionProtocolError",
    "DecodedExecution",
    "DecodedUIntCall",
    "UIntCallSpec",
    "completeness_check",
    "execute_uint_calls",
]
