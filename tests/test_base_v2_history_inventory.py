import pytest

from veltrix.base_archive_preflight import PreflightError
from veltrix.base_v2_history_inventory import (
    PAIR_CREATED_TOPIC,
    decode_pair_created,
    normalize_event_logs,
    validate_event_sequence,
    word_address,
    word_uint,
)


def _address_topic(address):
    return "0x" + "00" * 12 + address[2:].lower()


def _event(index, pair, token0, token1, block=10):
    return {
        "address": "0x" + "aa" * 20,
        "blockHash": "0x" + "bb" * 32,
        "blockNumber": hex(block),
        "transactionHash": "0x" + "cc" * 32,
        "transactionIndex": "0x0",
        "logIndex": hex(index - 1),
        "topics": [PAIR_CREATED_TOPIC, _address_topic(token0), _address_topic(token1)],
        "data": "0x" + word_address(pair) + word_uint(index),
    }


def test_decode_pair_created_extracts_pair_tokens_and_one_based_index():
    pair = "0x" + "11" * 20
    token0 = "0x" + "22" * 20
    token1 = "0x" + "33" * 20
    result = decode_pair_created(_event(7, pair, token0, token1))
    assert result == {
        "pair_index": 7, "pair": pair, "token0": token0, "token1": token1
    }


def test_decode_pair_created_rejects_wrong_topic_count():
    row = _event(1, "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20)
    row["topics"] = row["topics"][:2]
    with pytest.raises(PreflightError, match="two indexed token"):
        decode_pair_created(row)


def test_decode_pair_created_rejects_noncanonical_pair_address_word():
    row = _event(1, "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20)
    row["data"] = "0x" + "ff" * 12 + "11" * 20 + word_uint(1)
    with pytest.raises(PreflightError, match="canonical pair address"):
        decode_pair_created(row)


def test_decode_pair_created_rejects_unsorted_tokens():
    row = _event(1, "0x" + "11" * 20, "0x" + "33" * 20, "0x" + "22" * 20)
    with pytest.raises(PreflightError, match="unordered"):
        decode_pair_created(row)


def test_normalize_event_logs_rejects_wrong_factory_address():
    row = _event(1, "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20)
    with pytest.raises(PreflightError, match="does not match factory"):
        normalize_event_logs([row], "0x" + "dd" * 20, 10, 10, "test")


def test_normalize_event_logs_sorts_by_block_transaction_and_log():
    factory = "0x" + "aa" * 20
    first = _event(1, "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20, 10)
    second = _event(2, "0x" + "44" * 20, "0x" + "55" * 20, "0x" + "66" * 20, 11)
    rows = normalize_event_logs([second, first], factory, 10, 11, "test")
    assert [row["pair_index"] for row in rows] == [1, 2]


def test_validate_event_sequence_requires_complete_contiguous_indices():
    events = [
        {"pair_index": 1, "pair": "0x" + "11" * 20},
        {"pair_index": 2, "pair": "0x" + "22" * 20},
    ]
    validate_event_sequence(events, 2, "test")
    with pytest.raises(PreflightError, match="missing, duplicated"):
        validate_event_sequence([events[0], dict(events[1], pair_index=3)], 2, "test")


def test_validate_event_sequence_rejects_duplicate_pair_addresses():
    events = [
        {"pair_index": 1, "pair": "0x" + "11" * 20},
        {"pair_index": 2, "pair": "0x" + "11" * 20},
    ]
    with pytest.raises(PreflightError, match="duplicate pair address"):
        validate_event_sequence(events, 2, "test")
