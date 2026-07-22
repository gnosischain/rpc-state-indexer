import pytest

from rpc_state_indexer.domain import BlockRef
from rpc_state_indexer.execution.base import (
    ContractCall,
    RawCallResult,
    digest_raw_results,
)
from rpc_state_indexer.execution.errors import (
    DuplicateBatchResponseId,
    MissingBatchResponses,
    UnknownBatchResponseId,
)
from rpc_state_indexer.execution.legacy_rpc_batch import (
    build_batch_requests,
    decode_batch_responses,
)

ANCHOR = BlockRef(10, "0x" + "11" * 32, "0x" + "22" * 32, 99)
CALLS = [
    ContractCall("a", "0x" + "aa" * 20, b"\x01"),
    ContractCall("b", "0x" + "bb" * 20, b"\x02"),
]


def test_eip1898_requests_pin_exact_hash() -> None:
    requests, mapping = build_batch_requests(CALLS, ANCHOR, eip1898=True)
    assert mapping == {1: CALLS[0], 2: CALLS[1]}
    assert requests[0]["params"][1] == {
        "blockHash": ANCHOR.block_hash,
        "requireCanonical": True,
    }


def test_number_requests_pin_exact_number() -> None:
    requests, _ = build_batch_requests(CALLS, ANCHOR, eip1898=False)
    assert requests[0]["params"][1] == hex(ANCHOR.number)


def test_out_of_order_responses_are_matched_by_id() -> None:
    _, mapping = build_batch_requests(CALLS, ANCHOR, eip1898=True)
    responses = [
        {"jsonrpc": "2.0", "id": 2, "result": "0x02"},
        {"jsonrpc": "2.0", "id": 1, "result": "0x01"},
    ]
    results = decode_batch_responses(responses, mapping)
    assert [result.key for result in results] == ["a", "b"]
    assert [result.returndata for result in results] == [b"\x01", b"\x02"]


@pytest.mark.parametrize(
    ("responses", "error"),
    [
        ([{"id": 1, "result": "0x01"}], MissingBatchResponses),
        (
            [
                {"id": 1, "result": "0x01"},
                {"id": 1, "result": "0x01"},
                {"id": 2, "result": "0x02"},
            ],
            DuplicateBatchResponseId,
        ),
        (
            [
                {"id": 1, "result": "0x01"},
                {"id": 3, "result": "0x03"},
            ],
            UnknownBatchResponseId,
        ),
    ],
)
def test_response_id_errors_fail_the_whole_batch(
    responses: list[dict[str, object]], error: type[Exception]
) -> None:
    _, mapping = build_batch_requests(CALLS, ANCHOR, eip1898=True)
    with pytest.raises(error):
        decode_batch_responses(responses, mapping)


def test_item_error_is_not_fabricated_as_zero() -> None:
    _, mapping = build_batch_requests(CALLS[:1], ANCHOR, eip1898=True)
    (result,) = decode_batch_responses(
        [
            {
                "id": 1,
                "error": {"code": -32000, "message": "execution reverted"},
            }
        ],
        mapping,
    )
    assert result.success is False
    assert result.returndata == b""
    assert result.error_code == -32000


def test_result_digest_is_deterministic_but_order_sensitive() -> None:
    first = RawCallResult("a", True, b"\x00" * 32)
    second = RawCallResult("b", True, b"\x01" * 32)
    assert digest_raw_results((first, second)) == digest_raw_results((first, second))
    assert digest_raw_results((first, second)) != digest_raw_results((second, first))
