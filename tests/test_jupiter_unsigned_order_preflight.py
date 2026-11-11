import base64
import json
from types import SimpleNamespace

import pytest

from veltrix import jupiter_unsigned_order_preflight as cli


def fake_order():
    return SimpleNamespace(
        provider_id="jupiter-swap-v2",
        request_id="req-1",
        quote_id="quote-1",
        observed_at_epoch_ms=1000,
        taker="FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U",
        fee_payer="FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U",
        input_mint=cli.WSOL_MINT,
        output_mint=cli.USDC_MINT,
        input_amount_raw=5000000,
        output_amount_raw=424000,
        minimum_output_amount_raw=423000,
        price_impact_pct=0.1,
        fee_bps=2.0,
        router="metis",
        transaction_version=0,
        last_valid_block_height=123,
        recent_blockhash="blockhash",
        route_plan=(),
        signature_fee_lamports=5000,
        prioritization_fee_lamports=0,
        rent_fee_lamports=0,
        serialized_transaction_bytes=b"unsigned-tx",
        message_bytes=b"unsigned-message",
    )


def test_rejects_any_amount_other_than_exact_approved_005_sol(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["cli", "--amount-raw", "1000000", "--output", str(tmp_path / "x.json")])
    assert cli.main() == 2


def test_persists_unsigned_order_and_safety_flags(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["cli", "--amount-raw", "5000000", "--output", str(tmp_path / "order.json")])
    monkeypatch.setattr(cli, "settings", lambda: SimpleNamespace(jupiter_taker_public_key="FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U"))

    class FakeAdapter:
        def __init__(self, **kwargs):
            assert kwargs["api_key"] is None

        def order_exact_in_for_configured_taker(self, input_mint, output_mint, amount_raw, *, policy):
            assert input_mint == cli.WSOL_MINT
            assert output_mint == cli.USDC_MINT
            assert amount_raw == 5000000
            assert policy.allowed_routers == ("metis",)
            assert policy.require_self_paid_fees is True
            return fake_order()

    monkeypatch.setattr(cli, "JupiterSwapV2OrderAdapter", FakeAdapter)
    assert cli.main() == 0
    report = json.loads((tmp_path / "order.json").read_text())
    assert report["status"] == "unsigned_order_received"
    assert report["transaction_constructed"] is True
    assert report["transaction_signed"] is False
    assert report["transaction_submitted"] is False
    assert report["transaction_broadcast"] is False
    assert report["execution_ready"] is False
    assert base64.b64decode(report["unsigned_transaction_base64"]) == b"unsigned-tx"
    assert base64.b64decode(report["message_base64"]) == b"unsigned-message"


def test_provider_failure_is_recorded_without_claiming_order(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["cli", "--amount-raw", "5000000", "--output", str(tmp_path / "failure.json")])
    monkeypatch.setattr(cli, "settings", lambda: SimpleNamespace(jupiter_taker_public_key="FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U"))

    class FailedAdapter:
        def __init__(self, **kwargs):
            pass

        def order_exact_in_for_configured_taker(self, *args, **kwargs):
            raise RuntimeError("insufficient funds")

    monkeypatch.setattr(cli, "JupiterSwapV2OrderAdapter", FailedAdapter)
    assert cli.main() == 1
    report = json.loads((tmp_path / "failure.json").read_text())
    assert report["status"] == "failed"
    assert report["transaction_constructed"] is False
    assert report["transaction_signed"] is False
    assert report["execution_ready"] is False
