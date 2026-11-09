from dataclasses import replace

import pytest

from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.market_data import MarketDataUnavailable, SolanaMarketData

MINT = "So11111111111111111111111111111111111111112"


def event(*, pair="pool-a", liquidity=1000, volume=500, observed=10_000,
          token=MINT, chain="solana", created=9_000, price=0.0001):
    return MarketEvent(
        chain=chain,
        token_address=token,
        pair_address=pair,
        symbol="MEME",
        price_usd=price,
        liquidity_usd=liquidity,
        volume_h1_usd=volume,
        price_change_m5_pct=2.5,
        observed_at_ms=observed,
        pair_created_at_ms=created,
        source="dexscreener",
    )


class FakeMarketProvider:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def fetch_token_pairs(self, chain, token_addresses):
        self.calls.append((chain, token_addresses))
        return self.events


def test_selects_deepest_pool_and_preserves_pair_count():
    provider = FakeMarketProvider([
        event(pair="shallow", liquidity=500),
        event(pair="deep", liquidity=2500, volume=900),
    ])
    snapshot = SolanaMarketData(provider).fetch(MINT, now_ms=10_500)
    assert provider.calls == [("solana", [MINT])]
    assert snapshot.selected_pair_address == "deep"
    assert snapshot.selected_pool_liquidity_usd == 2500
    assert snapshot.pair_count == 2
    assert snapshot.pair_age_seconds == 1


def test_ignores_other_chains_tokens_and_stale_observations():
    provider = FakeMarketProvider([
        event(pair="other-chain", chain="base"),
        event(pair="other-token", token="11111111111111111111111111111111"),
        event(pair="stale", observed=1),
    ])
    with pytest.raises(MarketDataUnavailable, match="no fresh usable"):
        SolanaMarketData(provider).fetch(MINT, now_ms=10_000)


def test_marks_low_liquidity_and_missing_pair_age_without_claiming_safety():
    provider = FakeMarketProvider([
        event(liquidity=20, volume=0, created=None),
    ])
    snapshot = SolanaMarketData(
        provider, minimum_liquidity_usd=1000
    ).fetch(MINT, now_ms=10_500)
    assert snapshot.quality_flags == (
        "below_configured_minimum_liquidity",
        "zero_hourly_volume",
        "pair_creation_time_missing",
    )


def test_rejects_future_observations():
    provider = FakeMarketProvider([event(observed=11_000)])
    with pytest.raises(MarketDataUnavailable):
        SolanaMarketData(provider).fetch(MINT, now_ms=10_000)


def test_invalid_mint_is_rejected_before_provider_call():
    provider = FakeMarketProvider([])
    with pytest.raises(ValueError):
        SolanaMarketData(provider).fetch("not-a-mint")
    assert provider.calls == []


def test_non_finite_market_values_fail_closed():
    provider = FakeMarketProvider([replace(event(), liquidity_usd=float("nan"))])
    with pytest.raises(MarketDataUnavailable, match="liquidity_usd"):
        SolanaMarketData(provider).fetch(MINT, now_ms=10_500)
