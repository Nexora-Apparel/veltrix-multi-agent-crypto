import json

import pytest

from veltrix.solana_meme.pumpportal import PumpPortalDiscovery, PumpPortalError

MINT = "So11111111111111111111111111111111111111112"


class FakeWebSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        if not self.messages:
            raise RuntimeError("test stream exhausted")
        return self.messages.pop(0)

    def close(self):
        self.closed = True


def test_pumpportal_subscribes_only_to_new_token_events_and_normalizes_create():
    ws = FakeWebSocket([
        json.dumps({"txType": "create", "mint": MINT, "symbol": "TEST"}),
        json.dumps({"txType": "buy", "mint": MINT}),
        json.dumps({"txType": "create", "mint": "not-a-solana-mint"}),
    ])
    adapter = PumpPortalDiscovery(websocket_factory=lambda *a, **k: ws)
    result = list(adapter.stream_new_tokens(max_messages=3))
    assert ws.sent == [{"method": "subscribeNewToken"}]
    assert len(result) == 1
    assert result[0].mint == MINT
    assert result[0].source == "pumpportal-new-token"
    assert result[0].chain == "solana"
    assert ws.closed


def test_pumpportal_ignores_malformed_and_non_object_messages():
    ws = FakeWebSocket(["not-json", "[]", json.dumps({"txType": "create", "mint": MINT})])
    adapter = PumpPortalDiscovery(websocket_factory=lambda *a, **k: ws)
    assert len(list(adapter.stream_new_tokens(max_messages=3))) == 1


def test_pumpportal_connection_errors_are_wrapped():
    def fail(*args, **kwargs):
        raise OSError("offline")
    adapter = PumpPortalDiscovery(websocket_factory=fail)
    with pytest.raises(PumpPortalError, match="connection failed"):
        list(adapter.stream_new_tokens(max_messages=1))


def test_pumpportal_does_not_emit_non_create_or_untyped_events():
    ws = FakeWebSocket([
        json.dumps({"txType": "sell", "mint": MINT}),
        json.dumps({"mint": MINT}),
    ])
    adapter = PumpPortalDiscovery(websocket_factory=lambda *a, **k: ws)
    assert list(adapter.stream_new_tokens(max_messages=2)) == []


def test_pumpportal_zero_message_probe_does_not_connect():
    def fail(*args, **kwargs):
        raise AssertionError("must not connect")
    adapter = PumpPortalDiscovery(websocket_factory=fail)
    assert list(adapter.stream_new_tokens(max_messages=0)) == []
