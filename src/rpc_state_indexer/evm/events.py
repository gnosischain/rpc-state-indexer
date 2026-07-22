"""Load committed ABI fragments and derive event topics without explorer access."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_utils.crypto import keccak


class AbiDefinitionError(ValueError):
    """A committed ABI fragment does not match its configuration."""


@dataclass(frozen=True, slots=True)
class EventDefinition:
    abi_name: str
    event_name: str
    topic0: str
    indexed_types: tuple[str, ...]


class AbiRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._documents: dict[str, list[dict[str, Any]]] = {}

    def _load(self, abi_name: str) -> list[dict[str, Any]]:
        cached = self._documents.get(abi_name)
        if cached is not None:
            return cached
        if not abi_name or not abi_name.replace("_", "a").isalnum():
            raise AbiDefinitionError(f"invalid ABI name: {abi_name!r}")
        path = self.root / f"{abi_name}.json"
        if not path.is_file():
            raise AbiDefinitionError(f"ABI fragment does not exist: {path}")
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise AbiDefinitionError(f"cannot load ABI fragment: {path}") from exc
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise AbiDefinitionError(f"ABI fragment must be an array of objects: {path}")
        self._documents[abi_name] = value
        return value

    def event(self, abi_name: str, event_name: str) -> EventDefinition:
        matches = [
            item
            for item in self._load(abi_name)
            if item.get("type") == "event" and item.get("name") == event_name
        ]
        if len(matches) != 1:
            raise AbiDefinitionError(
                f"expected exactly one {event_name} event in {abi_name}.json"
            )
        event = matches[0]
        if event.get("anonymous") is not False:
            raise AbiDefinitionError(f"anonymous events are unsupported: {event_name}")
        raw_inputs = event.get("inputs")
        if not isinstance(raw_inputs, list):
            raise AbiDefinitionError(f"event inputs must be an array: {event_name}")
        types: list[str] = []
        indexed_types: list[str] = []
        for item in raw_inputs:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise AbiDefinitionError(f"malformed event input: {event_name}")
            input_type = item["type"]
            types.append(input_type)
            if item.get("indexed") is True:
                indexed_types.append(input_type)
            elif item.get("indexed") is not False:
                raise AbiDefinitionError(
                    f"event input has no boolean indexed flag: {event_name}"
                )
        signature = f"{event_name}({','.join(types)})"
        return EventDefinition(
            abi_name=abi_name,
            event_name=event_name,
            topic0="0x" + keccak(text=signature).hex(),
            indexed_types=tuple(indexed_types),
        )

    def validate_holder_topics(
        self,
        abi_name: str,
        event_name: str,
        holder_topics: list[int],
    ) -> EventDefinition:
        event = self.event(abi_name, event_name)
        for position in holder_topics:
            indexed_index = position - 1
            if indexed_index >= len(event.indexed_types):
                raise AbiDefinitionError(
                    f"{abi_name}.{event_name} has no indexed topic {position}"
                )
            if event.indexed_types[indexed_index] != "address":
                raise AbiDefinitionError(
                    f"{abi_name}.{event_name} topic {position} is not address"
                )
        return event
