import io
import json
import urllib.error

import pytest

from veltrix.base_archive_preflight import Rpc
from veltrix.base_archive_extended_acceptance import (
    PAIR_CREATED_TOPIC,
    block_by_tag,
    common_finalized_checkpoint,
    normalized_logs,
    inspect_provider,
    pair_created_index,
    validate_pair_created_sequence,
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



def test_common_finalized_checkpoint_accepts_different_provider_heads():
    primary = FakeRpc({
        ("eth_getBlockByNumber", json.dumps(["0x64", False], sort_keys=True)): {
            "number": "0x64", "hash": "0xABC"
        }
    })
    candidate = FakeRpc({
        ("eth_getBlockByNumber", json.dumps(["0x64", False], sort_keys=True)): {
            "number": "0x64", "hash": "0xabc"
        }
    })
    result = common_finalized_checkpoint(
        primary, candidate,
        {"number": 100, "hash": "0xprimary-head"},
        {"number": 120, "hash": "0xcandidate-head"},
    )
    assert result == {"number": 100, "hash": "0xabc"}


def test_common_finalized_checkpoint_rejects_common_height_hash_mismatch():
    primary = FakeRpc({
        ("eth_getBlockByNumber", json.dumps(["0x64", False], sort_keys=True)): {
            "number": "0x64", "hash": "0xaaa"
        }
    })
    candidate = FakeRpc({
        ("eth_getBlockByNumber", json.dumps(["0x64", False], sort_keys=True)): {
            "number": "0x64", "hash": "0xbbb"
        }
    })
    with pytest.raises(Exception, match="common finalized height"):
        common_finalized_checkpoint(
            primary, candidate,
            {"number": 100, "hash": "0xaaa"},
            {"number": 120, "hash": "0xccc"},
        )


def _pair_created_log(index):
    return {
        "data": "0x" + ("00" * 32) + index.to_bytes(32, "big").hex()
    }


def test_pair_created_sequence_requires_every_factory_pair_index():
    logs = [_pair_created_log(9), _pair_created_log(7), _pair_created_log(8)]
    validate_pair_created_sequence(logs, 6, 9, "test-provider")


def test_pair_created_sequence_rejects_missing_or_duplicate_indices():
    logs = [_pair_created_log(7), _pair_created_log(9), _pair_created_log(9)]
    with pytest.raises(Exception, match="indices do not reconcile"):
        validate_pair_created_sequence(logs, 6, 9, "test-provider")


def test_pair_created_index_rejects_wrong_event_data_length():
    with pytest.raises(Exception, match="expected 64 bytes"):
        pair_created_index({"data": "0x1234"})


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
        "blockNumber": "0x10",
        "transactionHash": "0xBBBB",
        "logIndex": "0x2",
        "topics": [PAIR_CREATED_TOPIC.upper()],
        "data": "0xABCD",
    }
    first = {
        "blockHash": "0xCAFE",
        "blockNumber": "0x10",
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
    rows = normalized_logs(rpc, "0x0000000000000000000000000000000000000001", 16, 16)
    assert [row["transactionHash"] for row in rows] == ["0xaaaa", "0xbbbb"]
    assert rows[0]["topics"] == [PAIR_CREATED_TOPIC]
    assert rows[0]["data"] == "0x1234"


def test_normalized_logs_reports_partial_backward_scan_coverage():
    factory = "0x0000000000000000000000000000000000000001"
    event = {
        "blockHash": "0xAA", "blockNumber": "0x1a",
        "transactionHash": "0xBB", "logIndex": "0x0",
        "topics": [PAIR_CREATED_TOPIC], "data": "0x",
    }
    rpc = FakeRpc({
        ("eth_getLogs", json.dumps([{
            "address": factory, "fromBlock": "0x15",
            "toBlock": "0x1e", "topics": [PAIR_CREATED_TOPIC],
        }], sort_keys=True)): [event],
    })
    coverage = {}
    rows = normalized_logs(
        rpc, factory, 11, 30, stop_after=1, coverage=coverage
    )
    assert len(rows) == 1
    assert coverage == {
        "queried_from_block": 21,
        "queried_to_block": 30,
    }


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
        normalized_logs(rpc, "0x0000000000000000000000000000000000000001", 16, 16)



def test_normalized_logs_chunks_ranges_to_ten_blocks_and_stops_at_target():
    factory = "0x0000000000000000000000000000000000000001"
    first_log = {
        "blockHash": "0xAA", "blockNumber": "0x1a",
        "transactionHash": "0xBB", "logIndex": "0x0",
        "topics": [PAIR_CREATED_TOPIC], "data": "0x",
    }
    second_log = {
        "blockHash": "0xCC", "blockNumber": "0x14",
        "transactionHash": "0xDD", "logIndex": "0x0",
        "topics": [PAIR_CREATED_TOPIC], "data": "0x",
    }
    rpc = FakeRpc({
        ("eth_getLogs", json.dumps([{
            "address": factory, "fromBlock": "0x15",
            "toBlock": "0x1e", "topics": [PAIR_CREATED_TOPIC],
        }], sort_keys=True)): [first_log],
        ("eth_getLogs", json.dumps([{
            "address": factory, "fromBlock": "0xb",
            "toBlock": "0x14", "topics": [PAIR_CREATED_TOPIC],
        }], sort_keys=True)): [second_log],
    })
    rows = normalized_logs(rpc, factory, 11, 30, stop_after=2)
    assert len(rows) == 2
    assert [call[1][0]["fromBlock"] for call in rpc.calls] == ["0x15", "0xb"]
    assert all(
        int(call[1][0]["toBlock"], 16) - int(call[1][0]["fromBlock"], 16) + 1 <= 10
        for call in rpc.calls
    )


def test_normalized_logs_rejects_non_pair_created_topic():
    factory = "0x0000000000000000000000000000000000000001"
    rpc = FakeRpc({
        ("eth_getLogs", json.dumps([{
            "address": factory, "fromBlock": "0x10",
            "toBlock": "0x10", "topics": [PAIR_CREATED_TOPIC],
        }], sort_keys=True)): [{
            "blockHash": "0xAA", "blockNumber": "0x10",
            "transactionHash": "0xBB", "logIndex": "0x0",
            "topics": ["0x" + "00" * 32], "data": "0x",
        }]
    })
    with pytest.raises(Exception, match="non-PairCreated"):
        normalized_logs(rpc, factory, 16, 16)



def test_rpc_retries_http_429_then_returns_result(monkeypatch):
    calls = {"count": 0}
    sleeps = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return b'{"jsonrpc":"2.0","id":2,"result":"0x2105"}'

    def fake_urlopen(request, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests",
                {"Retry-After": "1"}, io.BytesIO(b'{"error":"rate limited"}')
            )
        return Response()

    monkeypatch.setattr("veltrix.base_archive_preflight.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("veltrix.base_archive_preflight.time.sleep", sleeps.append)
    rpc = Rpc("https://rpc.example.invalid", "test-provider")

    assert rpc.call("eth_chainId", []) == "0x2105"
    assert calls["count"] == 2
    assert 1.0 in sleeps


def test_rpc_retries_json_rpc_rate_limit_code(monkeypatch):
    calls = {"count": 0}
    sleeps = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            return Response({"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}})
        return Response({"jsonrpc": "2.0", "id": 2, "result": "0x2105"})

    monkeypatch.setattr("veltrix.base_archive_preflight.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("veltrix.base_archive_preflight.time.sleep", sleeps.append)
    rpc = Rpc("https://rpc.example.invalid", "test-provider")

    assert rpc.call("eth_chainId", []) == "0x2105"
    assert calls["count"] == 2
    assert 2 in sleeps



def test_inspect_provider_reuses_captured_finalized_observation(monkeypatch):
    import veltrix.base_archive_extended_acceptance as acceptance

    class Provider(FakeRpc):
        label = "test-provider"

        def call(self, method, params):
            if method == "eth_chainId":
                return "0x2105"
            if method == "eth_getTransactionReceipt":
                return {"status": "0x1", "contractAddress": "0x" + "11" * 20,
                        "blockNumber": "0xa"}
            if method == "eth_getCode":
                return "0x6000"
            if method == "eth_call":
                return "0x0" if params[1] == "0xa" else "0x1"
            raise AssertionError(f"unexpected RPC call {method}")

    monkeypatch.setattr(
        acceptance, "block_by_tag",
        lambda *_: (_ for _ in ()).throw(AssertionError("finalized tag reread")),
    )
    monkeypatch.setattr(acceptance, "block_by_number", lambda _rpc, n: "0x" + f"{n:064x}")
    monkeypatch.setattr(
        acceptance, "normalized_logs",
        lambda *_args, **_kwargs: [{
            "blockNumber": 20, "transactionHash": "0x" + "22" * 32,
            "logIndex": 0, "data": "0x" + "00" * 64,
        }],
    )
    monkeypatch.setattr(acceptance, "validate_pair_created_sequence", lambda *_: None)

    captured = {"number": 20, "hash": "0x" + "33" * 32}
    report = inspect_provider(
        Provider({}), "0x" + "11" * 20, "0x" + "44" * 32, 10,
        [10, 20], captured,
    )
    assert report["finalized"] == captured
