from __future__ import annotations

import pytest

from veltrix.providers.base import ProviderError
from veltrix.providers.jupiter_swap import JupiterSwapV2QuoteProvider
from veltrix.solana_meme.sellability import WRAPPED_SOL_MINT

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FakeResponse:
    def __init__(self, body, error=None):
        self.body = body
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise RuntimeError("http failure")

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.body)


def payload(**overrides):
    body = {
        "inputMint": MINT,
        "outputMint": WRAPPED_SOL_MINT,
        "inAmount": "10000000",
        "outAmount": "50000000",
        "otherAmountThreshold": "49500000",
        "priceImpact": -0.25,
        "routePlan": [{"swapInfo": {"label": "Raydium"}}],
        "requestId": "jupiter-request-1",
        "transaction": None,
    }
    body.update(overrides)
    return body


def test_jupiter_v2_quote_only_request_omits_taker_and_never_uses_transaction():
    session = FakeSession(payload())
    provider = JupiterSwapV2QuoteProvider(
        session=session, clock_ms=lambda: 1_000,
        monotonic=lambda: 1.0, sleep=lambda _: None,
    )
    quote = provider.fetch_sell_quote(MINT, 10_000_000)
    assert quote.provider == "jupiter-swap-v2-order"
    assert quote.input_amount_raw == 10_000_000
    assert quote.output_mint == WRAPPED_SOL_MINT
    assert quote.quoted_output_raw == 50_000_000
    assert quote.minimum_output_raw == 49_500_000
    assert quote.route_plan_count == 1
    assert quote.price_impact_bps == 25.0
    params = session.calls[0][1]["params"]
    assert "taker" not in params
    assert "transaction" not in params
    assert session.calls[0][1]["headers"] == {"Accept": "application/json"}


def test_jupiter_provider_uses_optional_existing_api_key_and_rate_limits():
    session = FakeSession(payload())
    clock = {"now": 10.0}
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds
    provider = JupiterSwapV2QuoteProvider(
        api_key="existing-key", session=session,
        monotonic=lambda: clock["now"], sleep=sleep,
        clock_ms=lambda: 1_000,
    )
    provider.fetch_sell_quote(MINT, 10_000_000)
    provider.fetch_sell_quote(MINT, 10_000_000)
    assert sleeps == [pytest.approx(2.1)]
    assert session.calls[0][1]["headers"]["x-api-key"] == "existing-key"


def test_jupiter_no_route_is_normalized_without_fabricating_output():
    session = FakeSession(payload(routePlan=[]))
    provider = JupiterSwapV2QuoteProvider(
        session=session, clock_ms=lambda: 1_000,
        monotonic=lambda: 1.0, sleep=lambda _: None,
    )
    quote = provider.fetch_sell_quote(MINT, 10_000_000)
    assert quote.route_plan_count == 0
    assert quote.quoted_output_raw is None
    assert quote.minimum_output_raw is None
    assert quote.price_impact_bps is None


def test_jupiter_provider_rejects_mismatched_or_malformed_response():
    session = FakeSession(payload(outputMint=MINT))
    provider = JupiterSwapV2QuoteProvider(
        session=session, clock_ms=lambda: 1_000,
        monotonic=lambda: 1.0, sleep=lambda _: None,
    )
    with pytest.raises(ProviderError, match="mismatched token pair"):
        provider.fetch_sell_quote(MINT, 10_000_000)

    session.body = payload(inAmount="1")
    with pytest.raises(ProviderError, match="mismatched input"):
        provider.fetch_sell_quote(MINT, 10_000_000)


def test_jupiter_provider_requires_price_impact_and_quote_provenance():
    provider = JupiterSwapV2QuoteProvider(
        session=FakeSession(payload(priceImpact=None)),
        clock_ms=lambda: 1_000, monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )
    with pytest.raises(ProviderError):
        provider.fetch_sell_quote(MINT, 10_000_000)

    provider = JupiterSwapV2QuoteProvider(
        session=FakeSession(payload(requestId="")),
        clock_ms=lambda: 1_000, monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )
    with pytest.raises(ProviderError, match="provenance"):
        provider.fetch_sell_quote(MINT, 10_000_000)
