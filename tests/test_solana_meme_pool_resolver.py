from dataclasses import replace

import pytest

from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.pool_resolver import PoolResolutionUnavailable, SolanaPoolResolver

MINT = "So11111111111111111111111111111111111111112"


def _event(pair, liquidity, volume=100, observed=10_000):
    return MarketEvent(
        chain="solana", token_address=MINT, pair_address=pair,
        symbol="MEME", name="Meme", price_usd=0.001,
        liquidity_usd=liquidity, volume_h1_usd=volume,
        price_change_m5_pct=1.0, observed_at_ms=observed,
        pair_created_at_ms=9_000, source="dexscreener",
        dex_id="raydium", quote_token_symbol="SOL",
    )


class Provider:
    def __init__(self, events):
        self.events = events
    def fetch_token_pairs(self, chain, token_addresses):
        assert chain == "solana"
        assert token_addresses == [MINT]
        return self.events


def test_resolves_deepest_fresh_pool_and_returns_provenance():
    market = SolanaMarketData(
        Provider([_event("pool-low", 2_000), _event("pool-deep", 8_000)]),
        clock_ms=lambda: 10_500,
    )
    result = SolanaPoolResolver(market, minimum_liquidity_usd=1_000).resolve(MINT, now_ms=10_500)
    assert result.pair_address == "pool-deep"
    assert result.liquidity_usd == 8_000
    assert result.as_dict()["source"] == "dexscreener"
    assert result.as_dict()["synthetic"] is False


def test_fails_closed_when_no_fresh_market_pool_exists():
    market = SolanaMarketData(Provider([]), clock_ms=lambda: 10_500)
    with pytest.raises(PoolResolutionUnavailable):
        SolanaPoolResolver(market).resolve(MINT, now_ms=10_500)


def test_rejects_pool_below_liquidity_threshold():
    market = SolanaMarketData(Provider([_event("thin", 500)]), clock_ms=lambda: 10_500)
    with pytest.raises(PoolResolutionUnavailable, match="liquidity threshold"):
        SolanaPoolResolver(market, minimum_liquidity_usd=1_000).resolve(MINT, now_ms=10_500)


def test_rejects_zero_volume_pool():
    market = SolanaMarketData(Provider([_event("inactive", 5_000, volume=0)]), clock_ms=lambda: 10_500)
    with pytest.raises(PoolResolutionUnavailable, match="hourly volume"):
        SolanaPoolResolver(market, minimum_liquidity_usd=1_000).resolve(MINT, now_ms=10_500)


def test_rejects_invalid_mint_before_provider_call():
    class NeverCalled:
        def fetch_token_pairs(self, *args):
            raise AssertionError("provider must not be called")
    with pytest.raises(ValueError):
        SolanaPoolResolver(SolanaMarketData(NeverCalled())).resolve("not-a-mint")
