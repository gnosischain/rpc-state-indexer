import pytest

from rpc_state_indexer.domain import ObservationStatus
from rpc_state_indexer.evm.decoding import decode_uint256, hex_data_to_bytes


def test_valid_zero_is_successful_observation() -> None:
    result = decode_uint256(True, b"\x00" * 32)
    assert result.status is ObservationStatus.OK
    assert result.value == 0


@pytest.mark.parametrize("raw", [b"", b"\x01" * 31, b"\x01" * 33])
def test_non_32_byte_success_never_becomes_zero(raw: bytes) -> None:
    result = decode_uint256(True, raw)
    assert result.value is None
    assert result.status in {
        ObservationStatus.EMPTY_RETURN,
        ObservationStatus.MALFORMED_RETURN,
    }


def test_failed_call_discards_even_32_byte_payload() -> None:
    result = decode_uint256(False, b"\x00" * 32)
    assert result.status is ObservationStatus.REVERTED
    assert result.value is None


def test_uint_hex_input_is_supported_strictly() -> None:
    assert decode_uint256(True, "0x" + "00" * 31 + "2a").value == 42
    assert decode_uint256(True, "0x0").value is None


@pytest.mark.parametrize("value", ["", "01", "0x0", "0xzz"])
def test_hex_data_validation(value: str) -> None:
    with pytest.raises(ValueError):
        hex_data_to_bytes(value)
