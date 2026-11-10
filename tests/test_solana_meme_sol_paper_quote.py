from decimal import Decimal

import pytest

from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.sol_paper_quote import SolanaPaperQuoteAdapter, WSOL_MINT

MINT = "So11111111111111111111111111111111111111112"
TOKEN_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
NOW = 1_800_000_000_000


class FakeSolUsdProvider:
    def __init__(self, events):
        self.events = events

    def fetch_token_pairs(self, chain, token_addresses):
        assert chain == "solana"
        assert token_addresses == [WSOL_MINT]
        return self.events


def market_snapshot():
    event = MarketEvent(
        chain="solana", token_address=TOKEN_MINT, pair_address="token-pool",
        symbol="TEST", price_usd=0.50, liquidity_usd=200_000,
        volume_h1_usd=50_000, price_change_m5_pct=1.0,
        observed_at_ms=NOW - 1_000, pair_created_at_ms=NOW - 10_000,
        source="dexscreener", quote_token_symbol="SOL",
    )
    return SolanaMarketData(
        provider=type("P", (), {"fetch_token_pairs": lambda self, chain, mints: [event]})(),
        clock_ms=lambda: NOW,
    ).fetch([TOKEN_MINT]).snapshots[0]


def sol_event(*, price=100.0, observed=NOW - 500, liquidity=1_000_000, volume=100_000, quote="USDC"):
    return MarketEvent(
        chain="solana", token_address=WSOL_MINT, pair_address="sol-usdc-pool",
        symbol="SOL", price_usd=price, liquidity_usd=liquidity,
        volume_h1_usd=volume, price_change_m5_pct=0.1,
        observed_at_ms=observed, pair_created_at_ms=NOW - 100_000,
        source="dexscreener", quote_token_symbol=quote,
    )


def test_sol_quote_uses_fresh_token_and_sol_usd_observations(tmp_path):
    store = MarketSnapshotStore(str(tmp_path / "quote.sqlite3"))
    store.record(market_snapshot())
    adapter = SolanaPaperQuoteAdapter(
        store, FakeSolUsdProvider([sol_event()]), clock_ms=lambda: NOW,
    )
    quote = adapter.quote(TOKEN_MINT)
    assert quote.price_lamports == 5_000_000
    assert quote.token_price_usd == Decimal("0.5")
    assert quote.sol_price_usd == Decimal("100.0")
    assert quote.sol_source == "dexscreener"


def test_sol_quote_rejects_stale_or_untrusted_sol_market_data(tmp_path):
    store = MarketSnapshotStore(str(tmp_path / "quote.sqlite3"))
    store.record(market_snapshot())
    adapter = SolanaPaperQuoteAdapter(
        store, FakeSolUsdProvider([sol_event(observed=NOW - 90_000)]),
        clock_ms=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="no fresh"):
        adapter.quote(TOKEN_MINT)

    adapter = SolanaPaperQuoteAdapter(
        store, FakeSolUsdProvider([sol_event(quote="SOL")]), clock_ms=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="no fresh"):
        adapter.quote(TOKEN_MINT)


def test_sol_quote_rejects_stale_token_snapshot(tmp_path):
    store = MarketSnapshotStore(str(tmp_path / "quote.sqlite3"))
    store.record(market_snapshot())
    adapter = SolanaPaperQuoteAdapter(
        store, FakeSolUsdProvider([sol_event()]), max_age_ms=100,
        clock_ms=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="token USD market observation is stale"):
        adapter.quote(TOKEN_MINT)
