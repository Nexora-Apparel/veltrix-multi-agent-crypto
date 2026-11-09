from dataclasses import replace

import pytest

from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.market_data import (
    MarketDataUnavailable,
    SolanaMarketData,
)

MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MINT_OTHER = "11111111111111111111111111111111"


def event(
    mint=MINT_A,
    *,
    pair="pool-a",
    liquidity=1000,
    volume=500,
    observed=10_000,
    chain="solana",
    created=9_000,
    price=0.0001,
    **extra,
):
    values = dict(
        chain=chain,
        token_address=mint,
        pair_address=pair,
        symbol="MEME",
        name="Meme Token",
        price_usd=price,
        liquidity_usd=liquidity,
        volume_h1_usd=volume,
        price_change_m5_pct=2.5,
        observed_at_ms=observed,
        pair_created_at_ms=created,
        source="dexscreener",
        fdv_usd=1_000_000,
        market_cap_usd=900_000,
        volume_m5_usd=50,
        volume_h24_usd=5_000,
        price_change_h1_pct=-1.5,
        price_change_h24_pct=12.5,
        txns_m5_buys=10,
        txns_m5_sells=5,
        txns_h1_buys=100,
        txns_h1_sells=60,
        txns_h24_buys=900,
        txns_h24_sells=700,
        dex_id="raydium",
        quote_token_symbol="SOL",
    )
    values.update(extra)
    return MarketEvent(**values)


class FakeMarketProvider:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def fetch_token_pairs(self, chain, token_addresses):
        self.calls.append((chain, token_addresses))
        return self.events


def test_fetch_many_batches_mints_and_selects_deepest_pool_per_token():
    provider = FakeMarketProvider([
        event(pair="shallow", liquidity=500),
        event(pair="deep", liquidity=2500, volume=900),
        event(MINT_B, pair="other-token", liquidity=100),
    ])
    market = SolanaMarketData(provider, clock_ms=lambda: 10_500)

    batch = market.fetch_many([MINT_A, MINT_B, MINT_A])

    assert provider.calls == [("solana", [MINT_A, MINT_B])]
    assert set(batch.snapshots) == {MINT_A, MINT_B}
    assert batch.unavailable == {}
    snapshot = batch.snapshots[MINT_A]
    assert snapshot.selected_pair_address == "deep"
    assert snapshot.selected_pool_liquidity_usd == 2500
    assert snapshot.pair_count == 2
    assert snapshot.pair_age_seconds == 1
    assert snapshot.name == "Meme Token"
    assert snapshot.market_cap_usd == 900_000
    assert snapshot.fdv_usd == 1_000_000
    assert snapshot.txns_h1_buys == 100
    assert snapshot.txns_h1_sells == 60
    assert snapshot.dex_id == "raydium"
    assert snapshot.quote_token_symbol == "SOL"


def test_fetch_uses_same_batch_path_for_single_mint():
    provider = FakeMarketProvider([event()])
    snapshot = SolanaMarketData(provider).fetch(MINT_A, now_ms=10_500)
    assert snapshot.mint == MINT_A
    assert provider.calls == [("solana", [MINT_A])]


def test_ignores_other_chains_mints_and_stale_response_timestamps():
    provider = FakeMarketProvider([
        event(chain="base"),
        event(MINT_OTHER),
        event(pair="stale", observed=1),
    ])
    batch = SolanaMarketData(provider).fetch_many([MINT_A], now_ms=200_000)
    assert batch.snapshots == {}
    assert MINT_A in batch.unavailable


def test_marks_missing_market_fields_without_claiming_token_safety():
    provider = FakeMarketProvider([
        event(
            liquidity=20,
            volume=0,
            created=None,
            volume_m5_usd=None,
            volume_h24_usd=None,
            price_change_h1_pct=None,
            price_change_h24_pct=None,
            market_cap_usd=None,
            fdv_usd=None,
            txns_h1_buys=None,
            txns_h1_sells=None,
        )
    ])
    snapshot = SolanaMarketData(
        provider, minimum_liquidity_usd=1000
    ).fetch(MINT_A, now_ms=10_500)
    assert "below_configured_minimum_liquidity" in snapshot.quality_flags
    assert "zero_hourly_volume" in snapshot.quality_flags
    assert "pair_creation_time_missing" in snapshot.quality_flags
    assert "market_cap_missing" in snapshot.quality_flags
    assert "fdv_missing" in snapshot.quality_flags
    assert "hourly_transaction_counts_missing" in snapshot.quality_flags


def test_rejects_future_response_observations():
    provider = FakeMarketProvider([event(observed=11_000)])
    with pytest.raises(MarketDataUnavailable):
        SolanaMarketData(provider).fetch(MINT_A, now_ms=10_000)


def test_invalid_mint_is_rejected_before_provider_call():
    provider = FakeMarketProvider([])
    with pytest.raises(ValueError):
        SolanaMarketData(provider).fetch("not-a-mint")
    assert provider.calls == []


def test_non_finite_market_values_are_discarded_fail_closed():
    provider = FakeMarketProvider([replace(event(), liquidity_usd=float("nan"))])
    with pytest.raises(MarketDataUnavailable):
        SolanaMarketData(provider).fetch(MINT_A, now_ms=10_500)


def test_direct_snapshot_construction_rejects_invalid_price():
    provider = FakeMarketProvider([event()])
    snapshot = SolanaMarketData(provider).fetch(MINT_A, now_ms=10_500)
    with pytest.raises(ValueError):
        replace(snapshot, price_usd=float("nan"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_response_age_ms": 0},
        {"max_response_age_ms": True},
        {"max_response_age_ms": float("inf")},
        {"minimum_liquidity_usd": float("nan")},
        {"minimum_liquidity_usd": True},
    ],
)
def test_rejects_invalid_market_data_configuration(kwargs):
    with pytest.raises(ValueError):
        SolanaMarketData(**kwargs)

def test_preserves_quote_side_and_does_not_mislabel_base_price_change():
    quote_event = replace(
        event(),
        price_usd=100.0,
        price_change_m5_pct=None,
        price_change_h1_pct=None,
        price_change_h24_pct=None,
        fdv_usd=None,
        market_cap_usd=None,
        pool_token_side="quote",
    )
    snapshot = SolanaMarketData(
        FakeMarketProvider([quote_event])
    ).fetch(MINT_A, now_ms=10_500)

    assert snapshot.pool_token_side == "quote"
    assert snapshot.price_usd == 100.0
    assert snapshot.price_change_m5_pct is None
    assert "price_change_m5_missing" in snapshot.quality_flags
    assert "market_cap_missing" in snapshot.quality_flags
