from dataclasses import replace

import pytest

from veltrix.solana_meme.sellability import (
    SolanaSellQuoteObservation, SolanaSellabilityEvaluator, WRAPPED_SOL_MINT,
)

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FakeQuoteProvider:
    provider_id = "fake-jupiter"

    def __init__(self, quote=None, error=None):
        self.quote = quote or SolanaSellQuoteObservation(
            provider=self.provider_id, evidence_id="quote:abc", mint=MINT,
            output_mint=WRAPPED_SOL_MINT, input_amount_raw=10_000_000,
            quoted_output_raw=50_000_000, minimum_output_raw=49_000_000,
            route_plan_count=2, price_impact_bps=75.0,
            observed_at_ms=100_000, expires_at_ms=115_000,
        )
        self.error = error

    def fetch_sell_quote(self, mint, input_amount_raw):
        assert mint == MINT
        assert input_amount_raw == 10_000_000
        if self.error:
            raise self.error
        return self.quote


def evaluator(provider=None, **kwargs):
    return SolanaSellabilityEvaluator(
        provider or FakeQuoteProvider(), clock_ms=lambda: 105_000, **kwargs,
    )


def test_valid_sell_route_is_observed_but_never_certified_executable():
    result = evaluator().evaluate(
        MINT, total_supply_raw=1_000_000_000,
        input_amount_raw=10_000_000,
    )
    assert result.route_observed is True
    assert result.state == "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN"
    assert result.input_supply_fraction == 0.01
    assert result.execution_proven is False


def test_buy_direction_or_wrong_output_mint_is_rejected():
    provider = FakeQuoteProvider(replace(
        FakeQuoteProvider().quote, output_mint=MINT,
    ))
    result = evaluator(provider).evaluate(
        MINT, total_supply_raw=1_000_000_000,
        input_amount_raw=10_000_000,
    )
    assert result.state == "UNVERIFIED"
    assert "wrapped SOL" in result.errors[0]


def test_stale_quote_and_expired_quote_fail_closed():
    stale = FakeQuoteProvider(replace(
        FakeQuoteProvider().quote, observed_at_ms=1_000, expires_at_ms=120_000,
    ))
    result = evaluator(stale, max_age_ms=10_000).evaluate(
        MINT, total_supply_raw=1_000_000_000,
        input_amount_raw=10_000_000,
    )
    assert result.state == "UNVERIFIED"
    assert "stale" in result.errors[0]

    expired = FakeQuoteProvider(replace(
        FakeQuoteProvider().quote, expires_at_ms=104_999,
    ))
    result2 = evaluator(expired).evaluate(
        MINT, total_supply_raw=1_000_000_000,
        input_amount_raw=10_000_000,
    )
    assert result2.state == "UNVERIFIED"
    assert "expired" in result2.errors[0]


def test_no_route_is_distinct_from_provider_failure():
    no_route = FakeQuoteProvider(replace(
        FakeQuoteProvider().quote, route_plan_count=0,
        quoted_output_raw=None, minimum_output_raw=None,
    ))
    result = evaluator(no_route).evaluate(
        MINT, total_supply_raw=1_000_000_000,
        input_amount_raw=10_000_000,
    )
    assert result.state == "NO_SELL_ROUTE_OBSERVED"

    failed = evaluator(FakeQuoteProvider(error=RuntimeError("offline"))).evaluate(
        MINT, total_supply_raw=1_000_000_000,
        input_amount_raw=10_000_000,
    )
    assert failed.state == "UNVERIFIED"


def test_quote_size_and_price_impact_are_bounded():
    result = evaluator().evaluate(
        MINT, total_supply_raw=1_000_000_000,
        input_amount_raw=1_000_000,
    )
    assert result.state == "UNVERIFIED"
    assert "supply-fraction" in result.errors[0]

    high_impact = FakeQuoteProvider(replace(
        FakeQuoteProvider().quote, price_impact_bps=301.0,
    ))
    result2 = evaluator(high_impact).evaluate(
        MINT, total_supply_raw=1_000_000_000,
        input_amount_raw=10_000_000,
    )
    assert result2.state == "UNVERIFIED"
    assert "price impact" in result2.errors[0]


def test_invalid_amounts_are_rejected_before_provider_call():
    with pytest.raises(ValueError):
        evaluator().evaluate(
            MINT, total_supply_raw=0, input_amount_raw=10_000_000,
        )
    with pytest.raises(ValueError):
        evaluator().evaluate(
            MINT, total_supply_raw=1_000, input_amount_raw=2_000,
        )
