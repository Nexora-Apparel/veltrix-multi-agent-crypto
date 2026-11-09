from veltrix.core import Core
from veltrix.ledger import PaperBroker
from veltrix.providers.coordinator import CrossChainIntelligenceCoordinator
from veltrix.providers.intelligence import SmartWalletObservation, VerifiedIntelligenceAdapters


class WalletProvider:
    provider_id = "helius"
    def __init__(self, rows):
        self.rows = rows
    def fetch_wallet_observations(self, chain, token_addresses, since_ms):
        return self.rows


def row(wallet, tx):
    return SmartWalletObservation(
        provider="helius", chain="solana", token_address="mint",
        wallet_address=wallet, side="BUY", notional_usd=20,
        transaction_id=tx, observed_at_ms=990_000,
    )


def result():
    adapter = VerifiedIntelligenceAdapters(trusted_wallet_providers=["helius"])
    coordinator = CrossChainIntelligenceCoordinator(
        [WalletProvider([row("wallet-A", "tx-a"), row("wallet-B", "tx-b")])],
        adapter, trusted_wallets=["wallet-A", "wallet-B"],
    )
    return coordinator.collect("solana", ["mint"], 900_000, 1_000_000)


def test_verified_whale_cluster_cannot_bypass_missing_solana_risk_evidence(tmp_path):
    broker = PaperBroker(str(tmp_path / "verified-core.db"), 500, 10, 50)
    core = Core(broker)
    token = {
        "address": "mint", "chain": "solana", "price": 1.0,
        "volume_spike": True, "price_change_5m": 2.5,
        # Untrusted caller evidence must not be used.
        "smart_wallet_buys": ["fake-wallet-1", "fake-wallet-2"],
    }
    decision = core.evaluate_with_intelligence(token, result())
    assert decision is None
    assert broker.snapshot()["open_positions"] == []


def test_core_rejects_caller_whale_claims_when_intelligence_chain_mismatches(tmp_path):
    broker = PaperBroker(str(tmp_path / "mismatch-core.db"), 500, 10, 50)
    core = Core(broker)
    token = {
        "address": "mint", "chain": "base", "price": 1.0,
        "volume_spike": True, "price_change_5m": 2.5,
        "smart_wallet_buys": ["fake-wallet-1", "fake-wallet-2"],
    }
    decision = core.evaluate_with_intelligence(token, result())
    assert decision is None
    assert broker.snapshot()["open_positions"] == []
