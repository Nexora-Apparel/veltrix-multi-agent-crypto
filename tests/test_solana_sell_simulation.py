import base64

import pytest

from veltrix.providers.solana_sell_simulation import JupiterSolanaSellSimulationProvider

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TAKER = "11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


class Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, *, simulation_error=None, taker=TAKER):
        self.calls = []
        self.simulation_error = simulation_error
        self.taker = taker

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return Response({
            "inputMint": MINT,
            "outputMint": WSOL,
            "inAmount": "1000",
            "requestId": "order-123",
            "taker": self.taker,
            "transaction": base64.b64encode(b"unsigned-solana-transaction").decode(),
        })

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "context": {"slot": 123456},
                "value": {
                    "err": self.simulation_error,
                    "logs": ["Program log: simulated"],
                    "unitsConsumed": 12345,
                },
            },
        })


def provider(session):
    return JupiterSolanaSellSimulationProvider(
        "https://rpc.example/solana",
        TAKER,
        session=session,
        clock_ms=lambda: 1_800_000_000_000,
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )


def test_simulates_unsigned_sell_without_signing_or_submitting():
    session = FakeSession()
    result = provider(session).simulate_sell(MINT, 1000)

    assert result.state == "SIMULATION_SUCCEEDED"
    assert result.simulated_slot == 123456
    assert result.units_consumed == 12345
    assert result.signature_verification is False
    assert result.transaction_submitted is False
    assert [call[0] for call in session.calls] == ["GET", "POST"]

    order_params = session.calls[0][2]["params"]
    assert order_params["inputMint"] == MINT
    assert order_params["outputMint"] == WSOL
    assert order_params["taker"] == TAKER

    rpc_payload = session.calls[1][2]["json"]
    assert rpc_payload["method"] == "simulateTransaction"
    assert rpc_payload["params"][1]["sigVerify"] is False
    assert rpc_payload["params"][1]["replaceRecentBlockhash"] is True


def test_simulation_error_is_recorded_not_promoted_to_success():
    result = provider(FakeSession(simulation_error={"InstructionError": [2, "Custom"]})).simulate_sell(
        MINT, 1000,
    )
    assert result.state == "SIMULATION_FAILED"
    assert result.error is not None
    assert result.transaction_submitted is False


def test_rejects_missing_or_malformed_rpc_and_wallet_configuration():
    with pytest.raises(ValueError):
        JupiterSolanaSellSimulationProvider("http://rpc.example", TAKER)
    with pytest.raises(ValueError):
        JupiterSolanaSellSimulationProvider("https://rpc.example", "not-a-public-key")


def test_rejects_invalid_sell_amount_before_network_calls():
    session = FakeSession()
    with pytest.raises(ValueError):
        provider(session).simulate_sell(MINT, 0)
    assert session.calls == []
