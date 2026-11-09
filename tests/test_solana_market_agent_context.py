import pytest

from veltrix.solana_meme.market_agent_context import SolanaMarketAgentContextProvider
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore

MINT = "So11111111111111111111111111111111111111112"


def add_snapshot(store, *, observed, retrieved, volume, pair="pair-1", momentum=1.5):
    return store.record(SolanaMarketSnapshot(
        mint=MINT, selected_pair_address=pair, symbol="TEST",
        price_usd=1.0, selected_pool_liquidity_usd=50_000,
        selected_pool_volume_h1_usd=volume, price_change_m5_pct=momentum,
        pair_count=1, retrieved_at_ms=retrieved, provider_observed_at_ms=observed,
        pair_created_at_ms=100, quality_flags=(),
    ))


def test_market_context_detects_spike_from_five_same_pool_observations(tmp_path):
    store = MarketSnapshotStore(tmp_path / "market-context.sqlite3")
    for index in range(5):
        add_snapshot(store, observed=100_000 + index * 1_000,
                     retrieved=100_000 + index * 1_000, volume=100)
    add_snapshot(store, observed=106_000, retrieved=106_000, volume=250, momentum=2.5)
    provider = SolanaMarketAgentContextProvider(
        store, baseline_observations=5, clock_ms=lambda: 106_000,
    )
    context = provider(MINT, {"market": store.latest(MINT)})
    assert context.volume_spike is True
    assert context.price_change_5m_pct == 2.5
    assert context.provider == "veltrix-dexscreener-market-context"
    assert context.evidence_id.startswith("solana-market-context:")
    assert context.smart_wallet_buys == ()


def test_market_context_does_not_mix_different_pools(tmp_path):
    store = MarketSnapshotStore(tmp_path / "pool-change.sqlite3")
    for index in range(5):
        add_snapshot(store, observed=100_000 + index * 1_000,
                     retrieved=100_000 + index * 1_000, volume=100, pair="old-pair")
    add_snapshot(store, observed=106_000, retrieved=106_000, volume=500, pair="new-pair")
    context = SolanaMarketAgentContextProvider(
        store, baseline_observations=5, clock_ms=lambda: 106_000,
    )(MINT, {"market": store.latest(MINT)})
    assert context.volume_spike is False


def test_market_context_requires_fresh_latest_snapshot(tmp_path):
    store = MarketSnapshotStore(tmp_path / "stale-market.sqlite3")
    add_snapshot(store, observed=100_000, retrieved=100_000, volume=100)
    provider = SolanaMarketAgentContextProvider(
        store, clock_ms=lambda: 300_001, max_latest_age_ms=120_000,
    )
    with pytest.raises(ValueError, match="stale or future-dated"):
        provider(MINT, {"market": store.latest(MINT)})


def test_market_context_without_baseline_never_invents_spike(tmp_path):
    store = MarketSnapshotStore(tmp_path / "short-baseline.sqlite3")
    add_snapshot(store, observed=100_000, retrieved=100_000, volume=10_000)
    context = SolanaMarketAgentContextProvider(
        store, clock_ms=lambda: 100_000,
    )(MINT, {})
    assert context.volume_spike is False


def test_market_context_rejects_snapshot_superseded_after_decision_packet(tmp_path):
    store = MarketSnapshotStore(tmp_path / "superseded-market.sqlite3")
    add_snapshot(store, observed=100_000, retrieved=100_000, volume=100)
    supplied = store.latest(MINT)
    add_snapshot(store, observed=101_000, retrieved=101_000, volume=120)
    provider = SolanaMarketAgentContextProvider(store, clock_ms=lambda: 101_000)
    with pytest.raises(ValueError, match="no longer the persisted history head"):
        provider(MINT, {"market": supplied})
