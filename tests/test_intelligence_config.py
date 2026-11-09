from veltrix.config import Settings
from veltrix.providers.intelligence import VerifiedIntelligenceAdapters


def test_provider_settings_default_to_fail_closed_allowlists():
    configured = Settings()
    assert configured.trusted_wallet_providers == ()
    assert configured.trusted_safety_providers == ()
    assert configured.wallet_evidence_max_age_ms == 300_000
    assert configured.safety_evidence_max_age_ms == 300_000
    assert configured.alchemy_api_key == ""
    assert configured.tracked_evm_wallets == ()
    assert configured.alchemy_quote_tokens_base == ()
    assert configured.alchemy_quote_tokens_bsc == ()


def test_verified_adapters_can_be_built_from_application_settings():
    configured = Settings()
    adapters = VerifiedIntelligenceAdapters.from_settings(configured)
    assert adapters.trusted_wallet_providers == frozenset()
    assert adapters.trusted_safety_providers == frozenset()
    assert adapters.max_wallet_age_ms == 300_000
