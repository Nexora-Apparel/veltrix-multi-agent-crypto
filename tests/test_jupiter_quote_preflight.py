"""Tests for the fixed-size, quote-only Jupiter preflight command."""
import json
from dataclasses import dataclass

import pytest

from veltrix import jupiter_quote_preflight as preflight


@dataclass(frozen=True)
class FakeQuote:
    provider_id: str = "jupiter-swap-v2"
    request_id: str = "req-test"
    input_amount_raw: int = 5_000_000
    output_amount_raw: int = 250_000


def test_preflight_writes_quote_only_evidence_without_wallet_binding(monkeypatch, tmp_path, capsys):
    calls = {}

    class FakeAdapter:
        def __init__(self, *, api_key=None):
            calls["api_key"] = api_key

        def quote_exact_in(self, input_mint, output_mint, amount_raw, *, policy):
            calls.update(
                input_mint=input_mint,
                output_mint=output_mint,
                amount_raw=amount_raw,
                policy=policy,
            )
            return FakeQuote()

    monkeypatch.setattr(preflight, "JupiterSwapV2QuoteAdapter", FakeAdapter)
    monkeypatch.setenv("JUPITER_API_KEY", "configured-test-key")
    output = tmp_path / "nested" / "quote.json"
    monkeypatch.setattr(
        "sys.argv",
        ["jupiter_quote_preflight", "--amount-raw", "5000000", "--output", str(output)],
    )

    assert preflight.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "quote_received"
    assert report["quote"]["request_id"] == "req-test"
    assert calls["amount_raw"] == 5_000_000
    assert calls["input_mint"] == preflight.WSOL_MINT
    assert calls["output_mint"] == preflight.USDC_MINT
    assert calls["api_key"] == "configured-test-key"
    assert report["wallet_balance_required"] is False
    assert report["taker_wallet_bound"] is False
    assert report["transaction_constructed"] is False
    assert report["signed"] is False
    assert report["submitted"] is False
    assert report["execution_ready"] is False
    assert "configured-test-key" not in capsys.readouterr().out


def test_preflight_records_provider_failure_without_claiming_quote(monkeypatch, tmp_path):
    class FailingAdapter:
        def __init__(self, *, api_key=None):
            pass

        def quote_exact_in(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(preflight, "JupiterSwapV2QuoteAdapter", FailingAdapter)
    output = tmp_path / "quote.json"
    monkeypatch.setattr(
        "sys.argv",
        ["jupiter_quote_preflight", "--amount-raw", "5000000", "--output", str(output)],
    )

    assert preflight.main() == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["error"] == "provider unavailable"
    assert report["transaction_constructed"] is False
    assert report["execution_ready"] is False


def test_preflight_rejects_any_amount_other_than_approved_value(monkeypatch, tmp_path):
    class MustNotBeConstructed:
        def __init__(self, **kwargs):
            pytest.fail("adapter must not be constructed for a non-approved amount")

    monkeypatch.setattr(preflight, "JupiterSwapV2QuoteAdapter", MustNotBeConstructed)
    monkeypatch.setattr(
        "sys.argv",
        ["jupiter_quote_preflight", "--amount-raw", "1000000", "--output", str(tmp_path / "quote.json")],
    )
    assert preflight.main() == 2
    assert not (tmp_path / "quote.json").exists()
