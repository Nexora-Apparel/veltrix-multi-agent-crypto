from __future__ import annotations

from veltrix.live_raydium_pool_validation import HeliusHeaderSession


class CaptureSession:
    def __init__(self):
        self.args = None
        self.kwargs = None

    def post(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return "response"


def test_helius_api_key_uses_documented_x_api_key_header():
    session = HeliusHeaderSession("test-key")
    capture = CaptureSession()
    session._session = capture

    result = session.post(
        "https://mainnet.helius-rpc.com/",
        json={"jsonrpc": "2.0", "id": 1, "method": "getGenesisHash", "params": []},
        headers={"Content-Type": "application/json"},
        timeout=12,
        allow_redirects=False,
    )

    assert result == "response"
    assert capture.args == ("https://mainnet.helius-rpc.com/",)
    assert capture.kwargs["headers"]["X-Api-Key"] == "test-key"
    assert "api-key" not in capture.kwargs["headers"]
    assert "api-key" not in capture.args[0]


def test_helius_header_session_does_not_add_auth_header_without_key():
    session = HeliusHeaderSession(None)
    capture = CaptureSession()
    session._session = capture

    session.post(
        "https://mainnet.helius-rpc.com/",
        json={"jsonrpc": "2.0", "id": 1, "method": "getGenesisHash", "params": []},
        headers={"Content-Type": "application/json"},
        timeout=12,
        allow_redirects=False,
    )

    assert capture.kwargs["headers"] == {"Content-Type": "application/json"}
