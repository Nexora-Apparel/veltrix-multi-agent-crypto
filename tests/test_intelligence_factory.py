from types import SimpleNamespace

from veltrix.providers.coordinator import CrossChainIntelligenceCoordinator
from veltrix.providers.factory import build_intelligence_coordinator


def test_factory_builds_coordinator_with_trust_and_wallet_configuration():
    settings = SimpleNamespace(
        helius_api_key="", tracked_smart_wallets=("sol-wallet",),
        alchemy_api_key="", tracked_evm_wallets=("0xABC",),
        alchemy_quote_tokens_base=(), alchemy_quote_tokens_bsc=(),
        trusted_wallet_providers=("helius",),
        trusted_safety_providers=(),
        wallet_evidence_max_age_ms=300_000,
        safety_evidence_max_age_ms=300_000,
    )
    coordinator = build_intelligence_coordinator(settings, providers=[])
    assert isinstance(coordinator, CrossChainIntelligenceCoordinator)
    assert coordinator.aggregator.trusted_wallets == {"sol-wallet", "0xabc"}
    assert coordinator.adapters.trusted_wallet_providers == {"helius"}
