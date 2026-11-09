from dataclasses import dataclass

from veltrix.providers.coordinator import CrossChainIntelligenceCoordinator
from veltrix.providers.intelligence import (
    SmartWalletObservation,
    VerifiedIntelligenceAdapters,
)


@dataclass
class Provider:
    provider_id: str
    observations: tuple
    def fetch_wallet_observations(self, chain, token_addresses, since_ms):
        return self.observations


def observation(provider, chain, wallet, tx, token, at=990_000, notional=20):
    return SmartWalletObservation(
        provider=provider, chain=chain, token_address=token,
        wallet_address=wallet, side="BUY", notional_usd=notional,
        transaction_id=tx, observed_at_ms=at,
    )


def test_coordinator_deduplicates_same_wallet_tx_token_across_providers():
    shared = observation("helius", "solana", "wallet-A", "signature-1", "mint")
    duplicate = observation("alchemy", "solana", "wallet-A", "signature-1", "mint")
    other_wallet = observation("helius", "solana", "wallet-B", "signature-2", "mint")
    adapters = VerifiedIntelligenceAdapters(
        trusted_wallet_providers=["helius", "alchemy"]
    )
    coordinator = CrossChainIntelligenceCoordinator(
        [Provider("helius", (shared, other_wallet)),
         Provider("alchemy", (duplicate,))],
        adapters, trusted_wallets=["wallet-A", "wallet-B"],
    )
    result = coordinator.collect("solana", ["mint"], 900_000, 1_000_000)
    assert len(result.batch.accepted) == 2
    assert len(result.batch.rejected) == 1
    assert len(result.clusters) == 1
    assert set(result.clusters[0].wallet_addresses) == {"wallet-A", "wallet-B"}


def test_coordinator_keeps_chain_and_token_transaction_namespaces_distinct():
    rows = (
        observation("helius", "solana", "wallet-A", "same-tx", "mint-A"),
        observation("alchemy", "base", "wallet-A", "same-tx", "mint-A"),
        observation("helius", "solana", "wallet-A", "same-tx", "mint-B"),
    )
    adapters = VerifiedIntelligenceAdapters(
        trusted_wallet_providers=["helius", "alchemy"]
    )
    coordinator = CrossChainIntelligenceCoordinator(
        [Provider("helius", (rows[0], rows[2])),
         Provider("alchemy", (rows[1],))],
        adapters, trusted_wallets=["wallet-A"],
    )
    solana = coordinator.collect("solana", ["mint-A", "mint-B"], 900_000, 1_000_000)
    base = coordinator.collect("base", ["mint-A"], 900_000, 1_000_000)
    assert len(solana.batch.accepted) == 2
    assert len(base.batch.accepted) == 1


def test_coordinator_rejects_provider_identity_spoof_and_untrusted_provider():
    spoofed = observation("helius", "solana", "wallet-A", "tx", "mint")
    adapters = VerifiedIntelligenceAdapters(
        trusted_wallet_providers=["helius", "alchemy"]
    )
    coordinator = CrossChainIntelligenceCoordinator(
        [Provider("alchemy", (spoofed,)), Provider("unknown", ())],
        adapters, trusted_wallets=["wallet-A"],
    )
    result = coordinator.collect("solana", ["mint"], 900_000, 1_000_000)
    assert result.batch.accepted == ()
    assert any("identity mismatch" in reason for reason in result.batch.rejected)
    assert any("not trusted" in reason for reason in result.batch.rejected)


def test_coordinator_provider_failure_isolated_and_token_context_is_safe():
    good = observation("helius", "solana", "wallet-A", "tx-1", "mint")
    good2 = observation("helius", "solana", "wallet-B", "tx-2", "mint")
    class Broken:
        provider_id = "alchemy"
        def fetch_wallet_observations(self, *args):
            raise RuntimeError("secret should not leak")
    adapters = VerifiedIntelligenceAdapters(trusted_wallet_providers=["helius", "alchemy"])
    coordinator = CrossChainIntelligenceCoordinator(
        [Provider("helius", (good, good2)), Broken()],
        adapters, trusted_wallets=["wallet-A", "wallet-B"],
    )
    result = coordinator.collect("solana", ["mint"], 900_000, 1_000_000)
    assert len(result.clusters) == 1
    assert len(result.provider_failures) == 1
    original = {"address": "mint", "symbol": "TEST"}
    enriched = coordinator.enrich_token(original, result)
    assert original == {"address": "mint", "symbol": "TEST"}
    assert len(enriched["smart_wallet_buys"]) == 2
    assert enriched["whale_cluster_count"] == 1


def test_solana_base58_case_is_preserved_but_evm_hashes_are_case_normalized():
    adapters = VerifiedIntelligenceAdapters(
        trusted_wallet_providers=["helius", "alchemy"]
    )
    solana_rows = (
        observation("helius", "solana", "WalletA", "SignatureA", "MintA"),
        observation("helius", "solana", "walleta", "signaturea", "minta"),
    )
    solana = adapters.adapt_wallet_observations(solana_rows, 1_000_000)
    assert len(solana.accepted) == 2

    evm_rows = (
        observation("alchemy", "base", "0xAbCd", "0xABCDEF", "0xToken"),
        observation("alchemy", "base", "0xabcd", "0xabcdef", "0xtoken"),
    )
    evm = adapters.adapt_wallet_observations(evm_rows, 1_000_000)
    assert len(evm.accepted) == 1
    assert len(evm.rejected) == 1


def test_solana_mint_case_and_chain_are_checked_when_building_agent_context():
    adapters = VerifiedIntelligenceAdapters(trusted_wallet_providers=["helius"])
    rows = (
        observation("helius", "solana", "wallet-A", "sig-a", "MintA"),
        observation("helius", "solana", "wallet-B", "sig-b", "MintA"),
    )
    coordinator = CrossChainIntelligenceCoordinator(
        [Provider("helius", rows)], adapters,
        trusted_wallets=["wallet-A", "wallet-B"],
    )
    result = coordinator.collect("solana", ["MintA"], 900_000, 1_000_000)
    assert result.agent_context("MintA")["whale_cluster_count"] == 1
    assert result.agent_context("minta")["whale_cluster_count"] == 0
    wrong_chain = coordinator.enrich_token(
        {"address": "MintA", "chain": "base"}, result
    )
    assert wrong_chain["smart_wallet_buys"] == []
