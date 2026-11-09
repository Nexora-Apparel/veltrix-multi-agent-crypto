import base64

import pytest
from types import SimpleNamespace

import veltrix.providers.solana_sell_simulation as simulation_module

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


class FakeVersionedTransaction:
    signer = TAKER
    signatures_value = [bytes(64)]

    @classmethod
    def from_bytes(cls, raw):
        assert raw == b"unsigned-solana-transaction"
        return SimpleNamespace(
            message=SimpleNamespace(
                header=SimpleNamespace(num_required_signatures=1),
                account_keys=[cls.signer],
            ),
            signatures=cls.signatures_value,
        )


def provider(session, monkeypatch):
    monkeypatch.setattr(simulation_module, "VersionedTransaction", FakeVersionedTransaction)
    return JupiterSolanaSellSimulationProvider(
        "https://rpc.example/solana",
        TAKER,
        session=session,
        clock_ms=lambda: 1_800_000_000_000,
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )


def test_simulates_unsigned_sell_without_signing_or_submitting(monkeypatch):
    session = FakeSession()
    result = provider(session, monkeypatch).simulate_sell(MINT, 1000)

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


def test_simulation_error_is_recorded_not_promoted_to_success(monkeypatch):
    result = provider(FakeSession(simulation_error={"InstructionError": [2, "Custom"]}), monkeypatch).simulate_sell(
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


def test_rejects_invalid_sell_amount_before_network_calls(monkeypatch):
    session = FakeSession()
    with pytest.raises(ValueError):
        provider(session, monkeypatch).simulate_sell(MINT, 0)
    assert session.calls == []


def test_rejects_transaction_that_does_not_require_configured_taker_signature(monkeypatch):
    session = FakeSession()
    FakeVersionedTransaction.signer = "11111111111111111111111111111111"
    with pytest.raises(Exception, match="not a required transaction signer"):
        provider(session, monkeypatch).simulate_sell(MINT, 1000)
    assert [call[0] for call in session.calls] == ["GET"]
    FakeVersionedTransaction.signer = TAKER


def test_rejects_transaction_with_existing_signature(monkeypatch):
    session = FakeSession()
    FakeVersionedTransaction.signatures_value = [bytes([1]) + bytes(63)]
    with pytest.raises(Exception, match="not fully unsigned"):
        provider(session, monkeypatch).simulate_sell(MINT, 1000)
    assert [call[0] for call in session.calls] == ["GET"]
    FakeVersionedTransaction.signatures_value = [bytes(64)]
