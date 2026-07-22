"""Transport-neutral historical execution contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from rpc_state_indexer.domain import BlockRef, ExecutorKind


@dataclass(frozen=True, slots=True)
class ContractCall:
    key: str
    target: str
    calldata: bytes
    allow_failure: bool = True


@dataclass(frozen=True, slots=True)
class RawCallResult:
    key: str
    success: bool
    returndata: bytes
    error_code: int | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    executor_kind: ExecutorKind
    block_reference_kind: str
    anchor_hash: str
    provider_groups: tuple[str, ...]
    result_digest: str
    verified: bool


@dataclass(frozen=True, slots=True)
class VerifiedBatchResult:
    results: tuple[RawCallResult, ...]
    evidence: VerificationEvidence


class HistoricalCallExecutor(Protocol):
    async def execute(
        self,
        calls: Sequence[ContractCall],
        anchor: BlockRef,
    ) -> list[VerifiedBatchResult]: ...


def digest_raw_results(results: Sequence[RawCallResult]) -> str:
    digest = hashlib.sha256()
    for result in results:
        key = result.key.encode()
        message = (result.error_message or "").encode()
        digest.update(len(key).to_bytes(4, "big"))
        digest.update(key)
        digest.update(b"\x01" if result.success else b"\x00")
        digest.update(len(result.returndata).to_bytes(4, "big"))
        digest.update(result.returndata)
        code = result.error_code if result.error_code is not None else -(2**31)
        digest.update(code.to_bytes(8, "big", signed=True))
        digest.update(len(message).to_bytes(4, "big"))
        digest.update(message)
    return digest.hexdigest()

