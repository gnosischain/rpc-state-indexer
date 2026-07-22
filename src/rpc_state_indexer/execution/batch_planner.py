from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TypeVar

T = TypeVar("T")


def chunked(values: Sequence[T], size: int) -> Iterator[list[T]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    for offset in range(0, len(values), size):
        yield list(values[offset : offset + size])

