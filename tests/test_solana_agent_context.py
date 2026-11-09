from types import SimpleNamespace

import pytest

from veltrix.providers.coordinator import CrossChainIntelligenceResult
from veltrix.providers.intelligence import AdapterBatch
from veltrix.whales import WhaleCluster, WalletBuyEvent
from veltrix.solana_meme.agent_context import SolanaAgentContextProvider
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore

MINT = "So11111111111111111111111111111111111111112"
NOW = 10_000_000


class FakeCoordinator:
    def __init__(self, *, rejected=(), failures=()):
        self.providers = (object(),)
        self.adapters = SimpleNamespace(trusted_wallet_providers=frozenset({"helius"}))
        self.rejected = rejected
        self.failures = failures
        self.calls = []

    def collect(self, chain, token_addresses, *, since_ms, now_ms):
        self.calls.append((chain, token_addresses, since_ms, now_ms))
        accepted = (
            WalletBuyEvent(chain="solana", token_address=MINT, wallet_address="wallet-a",
                           observed_at_ms=NOW - 2_000, notional_usd=100, source="helius"),
            WalletBuyEvent(chain="solana", token_address=MINT, wallet_address="wallet-b",
                           observed_at_ms=NOW - 1_000, notional_usd=150, source="helius"),
        )
        batch = AdapterBatch(accepted=accepted, rejected=self.rejected)
        clusters = (
            WhaleCluster(
                chain="solana", token_address=MINT,
                wallet_addresses=("wallet-a", "wallet-b"),
                total_notional_usd=250, first_seen_ms=NOW - 2_000,
                last_seen_ms=NOW - 1_000,
            ),
        )
        return CrossChainIntelligenceResult(
            chain="solana", batch=batch, clusters=clusters,
            provider_failures=self.failures,
        )


def market_store(tmp_path, *, baseline_count=5):
    store = MarketSnapshotStore(str(tmp_path / "market.sqlite3"))
    for index in range(baseline_count, 0, -1):
        observed = NOW - index * 1_000
        store.record(SolanaMarketSnapshot(
            mint=MINT, selected_pair_address="pair-1", symbol="TEST",
            price_usd=1.0, selected_pool_liquidity_usd=100_000,
            selected_pool_volume_h1_usd=10_000, price_change_m5_pct=1.0,
            pair_count=1, retrieved_at_ms=observed,
            provider_observed_at_ms=observed, pair_created_at_ms=NOW - 60_000,
            quality_flags=(),
        ))
    store.record(SolanaMarketSnapshot(
        mint=MINT, selected_pair_address="pair-1", symbol="TEST",
        price_usd=1.0, selected_pool_liquidity_usd=100_000,
        selected_pool_volume_h1_usd=25_000, price_change_m5_pct=3.5,
        pair_count=1, retrieved_at_ms=NOW - 100,
        provider_observed_at_ms=NOW - 100, pair_created_at_ms=NOW - 60_000,
        quality_flags=(),
    ))
    return store


def test_context_uses_fresh_market_and_two_allowlisted_wallets(tmp_path):
    markets = market_store(tmp_path)
    coordinator = FakeCoordinator()
    provider = SolanaAgentContextProvider(
        coordinator, markets, clock_ms=lambda: NOW,
    )
    market = markets.latest(MINT)
    context = provider(MINT, {
        "market": market, "evaluation_now_ms": NOW,
    })
    assert context.provider == SolanaAgentContextProvider.provider_id
    assert context.mint == MINT
    assert context.volume_spike is True
    assert context.price_change_5m_pct == 3.5
    assert context.smart_wallet_buys == ("wallet-a", "wallet-b")
    assert context.observed_at_ms == NOW
    assert context.evidence_id.startswith("solana-agent-context:")
    assert coordinator.calls[0][0] == "solana"
    assert coordinator.calls[0][1] == (MINT,)
    assert coordinator.calls[0][2] == NOW - 300_000


def test_insufficient_market_history_does_not_invent_volume_spike(tmp_path):
    markets = market_store(tmp_path, baseline_count=2)
    provider = SolanaAgentContextProvider(
        FakeCoordinator(), markets, clock_ms=lambda: NOW,
    )
    context = provider(MINT, {
        "market": markets.latest(MINT), "evaluation_now_ms": NOW,
    })
    assert context.volume_spike is False


def test_rejected_wallet_observations_fail_closed(tmp_path):
    markets = market_store(tmp_path)
    provider = SolanaAgentContextProvider(
        FakeCoordinator(rejected=("wallet observation rejected",)),
        markets, clock_ms=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="rejected"):
        provider(MINT, {"market": markets.latest(MINT), "evaluation_now_ms": NOW})


def test_wallet_provider_failure_fails_closed(tmp_path):
    markets = market_store(tmp_path)
    provider = SolanaAgentContextProvider(
        FakeCoordinator(failures=("helius: request failed",)),
        markets, clock_ms=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="providers failed"):
        provider(MINT, {"market": markets.latest(MINT), "evaluation_now_ms": NOW})


def test_untrusted_helius_provider_cannot_build_agent_context(tmp_path):
    markets = market_store(tmp_path)
    coordinator = FakeCoordinator()
    coordinator.adapters.trusted_wallet_providers = frozenset()
    with pytest.raises(ValueError, match="allowlisted"):
        SolanaAgentContextProvider(coordinator, markets)
