from veltrix.providers.intelligence import (
    SmartWalletObservation,
    TokenSafetyObservation,
    VerifiedIntelligenceAdapters,
)
from veltrix.whales import WhaleFlowAggregator


def wallet(provider="wallet-indexer", wallet="w1", tx="tx1", **overrides):
    data = dict(provider=provider, chain="solana", token_address="mint",
                wallet_address=wallet, side="BUY", notional_usd=125.0,
                transaction_id=tx, observed_at_ms=1_000_000)
    data.update(overrides)
    return SmartWalletObservation(**data)


def safety(provider="audit-feed", **overrides):
    data = dict(provider=provider, token_address="mint", evidence_id="audit-1",
                observed_at_ms=1_000_000, liquidity_locked=True,
                dev_hold_pct=4.2, bundled_supply_pct=0.0)
    data.update(overrides)
    return TokenSafetyObservation(**data)


def test_wallet_adapter_accepts_trusted_fresh_buy_and_normalizes():
    adapter = VerifiedIntelligenceAdapters(["wallet-indexer"])
    batch = adapter.adapt_wallet_observations([wallet()], 1_000_100)
    assert len(batch.accepted) == 1
    event = batch.accepted[0]
    assert event.source == "wallet-indexer"
    assert event.notional_usd == 125.0
    assert batch.rejected == ()


def test_wallet_adapter_rejects_untrusted_stale_sell_and_duplicate():
    adapter = VerifiedIntelligenceAdapters(["wallet-indexer"], max_wallet_age_ms=100)
    batch = adapter.adapt_wallet_observations([
        wallet(provider="unknown"),
        wallet(wallet="w2", tx="old", observed_at_ms=999_000),
        wallet(wallet="w3", tx="sell", side="SELL"),
        wallet(wallet="w4", tx="dup"),
        wallet(wallet="w5", tx="dup"),
    ], 1_000_000)
    assert len(batch.accepted) == 1
    assert any("provider is not trusted" in reason for reason in batch.rejected)
    assert any("stale" in reason for reason in batch.rejected)
    assert any("not a buy" in reason for reason in batch.rejected)
    assert any("duplicate transaction" in reason for reason in batch.rejected)


def test_wallet_adapter_rejects_future_timestamp_bad_chain_and_nonfinite_notional():
    adapter = VerifiedIntelligenceAdapters(["wallet-indexer"])
    batch = adapter.adapt_wallet_observations([
        wallet(tx="future", observed_at_ms=1_000_001),
        wallet(tx="chain", chain="ethereum"),
        wallet(tx="nan", notional_usd=float("nan")),
    ], 1_000_000)
    assert batch.accepted == ()
    assert len(batch.rejected) == 3


def test_safety_adapter_requires_trusted_provider_and_fresh_complete_evidence():
    adapter = VerifiedIntelligenceAdapters(trusted_safety_providers=["audit-feed"])
    report = adapter.validate_safety_observation("mint", safety(), 1_000_100)
    assert report.passed
    untrusted = adapter.validate_safety_observation(
        "mint", safety(provider="market-data-api"), 1_000_100
    )
    assert not untrusted.passed
    assert "evidence provider is not trusted" in untrusted.reasons


def test_safety_adapter_rejects_missing_and_stale_evidence_with_sniper_reason():
    adapter = VerifiedIntelligenceAdapters(
        trusted_safety_providers=["audit-feed"], max_safety_age_ms=100
    )
    missing = adapter.validate_safety_observation("mint", None, 1_000_000)
    stale = adapter.validate_safety_observation("mint", safety(), 1_000_500)
    assert not missing.passed and not stale.passed
    assert any("no independent safety evidence" in r for r in missing.reasons)
    decision = adapter.sniper_decision(stale)
    assert not decision.accepted
    assert any(reason.startswith("sniper:") for reason in decision.reasons)


def test_whale_aggregator_consumes_only_adapter_accepted_events():
    adapter = VerifiedIntelligenceAdapters(["wallet-indexer"])
    batch = adapter.adapt_wallet_observations([
        wallet(wallet="w1", tx="tx1"),
        wallet(wallet="w2", tx="tx2", observed_at_ms=1_000_050),
    ], 1_000_100)
    clusters = WhaleFlowAggregator(["w1", "w2"]).aggregate(batch.accepted, 1_000_100)
    assert len(clusters) == 1
    decision = adapter.whale_decision(batch, len(clusters))
    assert decision.accepted


def test_wallet_adapter_does_not_crash_on_malformed_provider_fields():
    adapter = VerifiedIntelligenceAdapters(["wallet-indexer"])
    batch = adapter.adapt_wallet_observations([
        wallet(tx="bad-side", side=None),
        wallet(tx="bad-time", observed_at_ms="yesterday"),
    ], 1_000_000)
    assert batch.accepted == ()
    assert len(batch.rejected) == 2


class WalletFeed:
    provider_id = "wallet-indexer"
    def __init__(self, observations):
        self.observations = observations
    def fetch_wallet_observations(self, chain, token_addresses, since_ms):
        return self.observations


class SafetyFeed:
    provider_id = "audit-feed"
    def __init__(self, observation):
        self.observation = observation
    def fetch_safety_observation(self, chain, token_address):
        return self.observation


def test_provider_contract_collects_only_matching_wallet_identity():
    adapter = VerifiedIntelligenceAdapters(["wallet-indexer"])
    feed = WalletFeed([
        wallet(provider="wallet-indexer", tx="trusted"),
        wallet(provider="spoofed-provider", tx="spoofed"),
    ])
    batch = adapter.collect_wallet_observations(
        feed, "solana", ["mint"], 999_000, 1_000_100
    )
    assert len(batch.accepted) == 1
    assert any("provider identity mismatch" in reason for reason in batch.rejected)


def test_provider_contract_fails_closed_on_untrusted_or_failed_wallet_feed():
    adapter = VerifiedIntelligenceAdapters(["wallet-indexer"])
    class UntrustedFeed(WalletFeed):
        provider_id = "unknown"
    assert adapter.collect_wallet_observations(
        UntrustedFeed([]), "solana", ["mint"], 0, 1_000_000
    ).accepted == ()

    class BrokenFeed:
        provider_id = "wallet-indexer"
        def fetch_wallet_observations(self, chain, token_addresses, since_ms):
            raise RuntimeError("upstream unavailable")
    batch = adapter.collect_wallet_observations(
        BrokenFeed(), "solana", ["mint"], 0, 1_000_000
    )
    assert batch.accepted == ()
    assert "whale: trusted provider request failed" in batch.rejected


def test_provider_contract_fails_closed_on_safety_identity_mismatch():
    adapter = VerifiedIntelligenceAdapters(trusted_safety_providers=["audit-feed"])
    report = adapter.collect_safety_observation(
        SafetyFeed(safety(provider="different-feed")),
        "solana", "mint", 1_000_100
    )
    assert not report.passed
    assert "evidence provider is not trusted" in report.reasons
