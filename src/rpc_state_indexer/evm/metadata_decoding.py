"""Strict decoding of ERC-20 display metadata (symbol/name/decimals).

Display metadata is the one place where the token catalog meets contracts that predate the
standard it is named after. `symbol()` and `name()` are specified to return `string`, but
tokens deployed before ABI-encoded dynamic types were settled (MKR and its contemporaries)
return a raw, NUL-padded `bytes32`. Both encodings must be accepted, and anything that is
neither must be reported as unresolved rather than guessed at.

As everywhere else in this codebase, a failure is never coerced into a value: an
undecodable return yields ``None`` plus a reason, never an empty string or zero.
"""

from __future__ import annotations

from dataclasses import dataclass

from rpc_state_indexer.evm.decoding import hex_data_to_bytes

_WORD = 32
# ERC-20 names/symbols are short by construction. A longer claim is either a broken encoder
# or a contract trying to smuggle a payload into a label; either way it is not usable.
MAX_TEXT_LENGTH = 128


@dataclass(frozen=True, slots=True)
class TextObservation:
    """A decoded string field, or an explicit absence with its reason."""

    value: str | None
    encoding: str  # "string" | "bytes32" | "absent"
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.value is not None


def _clean_text(raw: bytes) -> str | None:
    """UTF-8 decode, reject control characters, collapse surrounding whitespace."""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    # Control characters (including embedded NULs past the first) mean the bytes were not
    # really text; a label containing them would corrupt every downstream display.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        return None
    text = text.strip()
    if not text or len(text) > MAX_TEXT_LENGTH:
        return None
    return text


def _decode_dynamic_string(raw: bytes) -> str | None:
    """Decode a canonical ABI dynamic string: offset word, length word, payload."""

    if len(raw) < 2 * _WORD:
        return None
    offset = int.from_bytes(raw[:_WORD], "big")
    # The offset is almost always exactly 0x20; anything unaligned or out of range is not
    # a string this decoder is willing to guess at.
    if offset % _WORD or not 0 < offset <= len(raw) - _WORD:
        return None
    length = int.from_bytes(raw[offset : offset + _WORD], "big")
    if length > MAX_TEXT_LENGTH:
        return None
    body_start = offset + _WORD
    if body_start + length > len(raw):
        return None
    return _clean_text(raw[body_start : body_start + length])


def _decode_bytes32_text(raw: bytes) -> str | None:
    """Decode a legacy bytes32 label: right-NUL-padded ASCII in a single word."""

    if len(raw) != _WORD:
        return None
    trimmed = raw.rstrip(b"\x00")
    if not trimmed:
        return None
    return _clean_text(trimmed)


def decode_text_return(success: bool, returndata: bytes | str) -> TextObservation:
    """Decode symbol()/name(), accepting both the modern and legacy encodings."""

    try:
        raw = (
            hex_data_to_bytes(returndata)
            if isinstance(returndata, str)
            else bytes(returndata)
        )
    except (TypeError, ValueError) as exc:
        return TextObservation(None, "absent", f"malformed return: {exc}")
    if not success:
        return TextObservation(None, "absent", "call reverted")
    if not raw:
        return TextObservation(None, "absent", "empty return")

    # bytes32 first when the return is exactly one word: a legacy label is unambiguous at
    # that length, whereas the dynamic decoder would read the label bytes as an offset.
    if len(raw) == _WORD:
        legacy = _decode_bytes32_text(raw)
        if legacy is not None:
            return TextObservation(legacy, "bytes32")
        return TextObservation(None, "absent", "single word is neither text nor a string head")

    dynamic = _decode_dynamic_string(raw)
    if dynamic is not None:
        return TextObservation(dynamic, "string")
    return TextObservation(None, "absent", "return matches no known string encoding")


def decode_decimals_return(success: bool, returndata: bytes | str) -> int | None:
    """Decode decimals() as a canonical uint8 word, or None when unobservable.

    Returning None for "not observed" is load-bearing: 0 is a legitimate answer for some
    tokens, so the caller must be able to tell the two apart.
    """

    try:
        raw = (
            hex_data_to_bytes(returndata)
            if isinstance(returndata, str)
            else bytes(returndata)
        )
    except (TypeError, ValueError):
        return None
    if not success or len(raw) != _WORD:
        return None
    value = int.from_bytes(raw, "big")
    # uint8 by ABI; and a token claiming more decimals than a uint256 can express
    # (10**78 overflows) is misreporting regardless of what the type allows.
    if value > 77:
        return None
    return value


__all__ = [
    "MAX_TEXT_LENGTH",
    "TextObservation",
    "decode_decimals_return",
    "decode_text_return",
]
