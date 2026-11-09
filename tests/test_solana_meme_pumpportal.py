import json
import threading

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


def test_subscribes_only_to_free_new_token_feed_and_normalizes_create():
    ws = FakeWebSocket([
        json.dumps({"txType": "create", "mint": MINT, "symbol": "TEST"}),
        json.dumps({"txType": "buy", "mint": MINT}),
        json.dumps({"txType": "create", "mint": "not-a-mint"}),
    ])
    adapter = PumpPortalDiscovery(
        websocket_factory=lambda *args, **kwargs: ws,
        clock_ms=lambda: 1_800_000_000_000,
    )

    result = list(adapter.stream_new_tokens(max_messages=3))

    assert ws.sent == [{"method": "subscribeNewToken"}]
    assert len(result) == 1
    assert result[0].mint == MINT
    assert result[0].source == "pumpportal-new-token"
    assert result[0].discovered_at_ms == 1_800_000_000_000
    assert ws.closed


def test_ignores_malformed_and_non_object_messages():
    ws = FakeWebSocket([
        "not-json",
        "[]",
        json.dumps({"txType": "create", "mint": MINT}),
    ])
    adapter = PumpPortalDiscovery(websocket_factory=lambda *a, **k: ws)
    assert len(list(adapter.stream_new_tokens(max_messages=3))) == 1


def test_connection_errors_are_wrapped():
    def fail(*args, **kwargs):
        raise OSError("offline")

    adapter = PumpPortalDiscovery(websocket_factory=fail)
    with pytest.raises(PumpPortalError, match="connection failed"):
        list(adapter.stream_new_tokens(max_messages=1))


def test_stream_errors_are_wrapped_and_socket_is_closed():
    class BrokenSocket(FakeWebSocket):
        def recv(self):
            raise OSError("socket lost")

    ws = BrokenSocket([])
    adapter = PumpPortalDiscovery(websocket_factory=lambda *a, **k: ws)
    with pytest.raises(PumpPortalError, match="stream failed"):
        list(adapter.stream_new_tokens(max_messages=1))
    assert ws.closed


def test_zero_message_probe_does_not_connect():
    def fail(*args, **kwargs):
        raise AssertionError("must not connect")

    adapter = PumpPortalDiscovery(websocket_factory=fail)
    assert list(adapter.stream_new_tokens(max_messages=0)) == []


@pytest.mark.parametrize("value", [-1, True, 1.5, "2"])
def test_rejects_invalid_message_limits(value):
    adapter = PumpPortalDiscovery(websocket_factory=lambda *a, **k: None)
    with pytest.raises(ValueError):
        list(adapter.stream_new_tokens(max_messages=value))


def test_stop_event_prevents_reading_messages():
    ws = FakeWebSocket([json.dumps({"txType": "create", "mint": MINT})])
    stop = threading.Event()
    stop.set()
    adapter = PumpPortalDiscovery(websocket_factory=lambda *a, **k: ws)
    assert list(adapter.stream_new_tokens(stop_event=stop)) == []
    assert ws.closed

@pytest.mark.parametrize("timeout", [0, -1, True, "30", float("nan"), float("inf")])
def test_rejects_invalid_timeout_configuration(timeout):
    with pytest.raises(ValueError):
        PumpPortalDiscovery(timeout_seconds=timeout)
