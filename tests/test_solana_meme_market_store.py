from dataclasses import replace

import pytest

from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore

MINT = "So11111111111111111111111111111111111111112"


def event(observed, price=0.001, pair="pool-1"):
    return MarketEvent(
        chain="solana",
        token_address=MINT,
        pair_address=pair,
        symbol="MEME",
        price_usd=price,
        liquidity_usd=5000,
        volume_h1_usd=1200,
        price_change_m5_pct=-3.5,
        observed_at_ms=observed,
        pair_created_at_ms=1000,
        source="dexscreener",
    )


class Provider:
    def __init__(self, events):
        self.events = events

    def fetch_token_pairs(self, chain, token_addresses):
        return self.events


def snapshot(retrieved=2000, observed=1900, price=0.001, pair="pool-1"):
    return SolanaMarketData(Provider([event(observed, price, pair)])).fetch(
        MINT, now_ms=retrieved
    )


def test_store_is_append_only_and_deduplicates_same_pair_response(tmp_path):
    store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    assert store.record(snapshot())
    assert not store.record(snapshot(retrieved=2100))
    assert store.count(MINT) == 1

    assert store.record(snapshot(retrieved=3000, observed=2900, price=0.0015))
    assert store.count(MINT) == 2


def test_latest_and_history_preserve_all_intelligence_fields(tmp_path):
    store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    store.record(snapshot(retrieved=2000, observed=1900))
    store.record(snapshot(retrieved=3000, observed=2900, price=0.0015))

    latest = store.latest(MINT)
    history = store.history(MINT)
    assert latest["retrieved_at_ms"] == 3000
    assert latest["price_usd"] == 0.0015
    assert [row["retrieved_at_ms"] for row in history] == [3000, 2000]
    assert history[0]["quality_flags"] == []
    assert history[0]["pair_address"] == "pool-1"


def test_distinct_selected_pairs_are_preserved(tmp_path):
    store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    assert store.record(snapshot(retrieved=2000, observed=1900, pair="pool-a"))
    assert store.record(snapshot(retrieved=2000, observed=1900, pair="pool-b"))
    assert store.count(MINT) == 2


def test_store_rejects_untyped_data_and_invalid_history_limit(tmp_path):
    store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    with pytest.raises(TypeError):
        store.record({"mint": MINT})
    with pytest.raises(ValueError):
        store.history(MINT, limit=0)
    with pytest.raises(ValueError):
        MarketSnapshotStore(":memory:")
