from dataclasses import replace

import pytest

from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore

MINT = "So11111111111111111111111111111111111111112"


def snapshot(retrieved=2000, observed=1900, pair="pool-1"):
    return SolanaMarketSnapshot(
        mint=MINT,
        selected_pair_address=pair,
        symbol="MEME",
        price_usd=0.001,
        selected_pool_liquidity_usd=5000,
        selected_pool_volume_h1_usd=1200,
        price_change_m5_pct=-3.5,
        pair_count=2,
        retrieved_at_ms=retrieved,
        provider_observed_at_ms=observed,
        pair_created_at_ms=1000,
        quality_flags=("low_liquidity",),
    )


def test_store_is_append_only_and_deduplicates_same_provider_observation(tmp_path):
    store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    assert store.record(snapshot())
    assert not store.record(snapshot(retrieved=2100))
    assert store.count(MINT) == 1

    assert store.record(snapshot(retrieved=3000, observed=2900))
    assert store.count(MINT) == 2


def test_latest_and_history_are_newest_first(tmp_path):
    store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    store.record(snapshot(retrieved=2000, observed=1900))
    store.record(snapshot(retrieved=3000, observed=2900))

    latest = store.latest(MINT)
    history = store.history(MINT)
    assert latest["retrieved_at_ms"] == 3000
    assert [row["retrieved_at_ms"] for row in history] == [3000, 2000]
    assert history[0]["quality_flags"] == ["low_liquidity"]


def test_store_rejects_untyped_data_and_invalid_history_limit(tmp_path):
    store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    with pytest.raises(TypeError):
        store.record({"mint": MINT})
    with pytest.raises(ValueError):
        store.history(MINT, limit=0)
