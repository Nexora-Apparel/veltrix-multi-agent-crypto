import json

import pytest

from veltrix.base_archive_extended_acceptance import (
    PAIR_CREATED_TOPIC,
    block_by_tag,
    normalized_logs,
    quantity,
)


class FakeRpc:
    label = "test-provider"

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        key = (method, json.dumps(params, sort_keys=True))
        if key in self.responses:
            value = self.responses[key]
            return value() if callable(value) else value
        raise AssertionError(f"Unexpected RPC call: {method} {params}")


def test_quantity_accepts_hex_quantity():
    assert quantity("0x349eb47", "block") == 0x349EB47


@pytest.mark.parametrize("value", [None, 12, "349eb47", "0xnot-hex"])
def test_quantity_rejects_malformed_values(value):
    with pytest.raises(Exception):
        quantity(value, "block")


def test_block_by_tag_normalizes_number_and_hash():
    rpc = FakeRpc({
        ("eth_getBlockByNumber", json.dumps(["finalized", False], sort_keys=True)): {
            "number": "0x100", "hash": "0xABCDEF"
        }
    })
    assert block_by_tag(rpc, "finalized") == {
        "number": 256, "hash": "0xabcdef"
    }


def test_normalized_logs_sorts_and_normalizes_log_fields():
    second = {
        "blockHash": "0xBEEF",
        "transactionHash": "0xBBBB",
        "logIndex": "0x2",
        "topics": [PAIR_CREATED_TOPIC.upper()],
        "data": "0xABCD",
    }
    first = {
        "blockHash": "0xCAFE",
        "transactionHash": "0xAAAA",
        "logIndex": "0x1",
        "topics": [PAIR_CREATED_TOPIC],
        "data": "0x1234",
    }
    rpc = FakeRpc({
        ("eth_getLogs", json.dumps([{
            "address": "0x0000000000000000000000000000000000000001",
            "fromBlock": "0x10",
            "toBlock": "0x10",
            "topics": [PAIR_CREATED_TOPIC],
        }], sort_keys=True)): [second, first]
    })
    rows = normalized_logs(rpc, "0x0000000000000000000000000000000000000001", 16)
    assert [row["transactionHash"] for row in rows] == ["0xaaaa", "0xbbbb"]
    assert rows[0]["topics"] == [PAIR_CREATED_TOPIC]
    assert rows[0]["data"] == "0x1234"


def test_normalized_logs_rejects_malformed_rows():
    rpc = FakeRpc({
        ("eth_getLogs", json.dumps([{
            "address": "0x0000000000000000000000000000000000000001",
            "fromBlock": "0x10",
            "toBlock": "0x10",
            "topics": [PAIR_CREATED_TOPIC],
        }], sort_keys=True)): ["not-an-object"]
    })
    with pytest.raises(Exception, match="malformed log row"):
        normalized_logs(rpc, "0x0000000000000000000000000000000000000001", 16)
