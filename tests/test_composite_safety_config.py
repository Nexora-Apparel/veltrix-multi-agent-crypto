from types import SimpleNamespace

from veltrix.composite_safety import composite_validator_from_settings
from veltrix.providers.intelligence import VerifiedIntelligenceAdapters


def test_settings_factory_uses_separate_provider_allowlists():
    settings = SimpleNamespace(
        trusted_liquidity_providers=("lock-provider",),
        trusted_developer_providers=("dev-provider",),
        trusted_bundle_providers=("bundle-provider",),
        safety_evidence_max_age_ms=123_000,
    )
    validator = composite_validator_from_settings(settings)
    assert validator.trusted_liquidity == {"lock-provider"}
    assert validator.trusted_developer == {"dev-provider"}
    assert validator.trusted_bundle == {"bundle-provider"}
    assert validator.max_age_ms == 123_000


def test_generic_adapter_does_not_silently_trust_composite_claims():
    adapter = VerifiedIntelligenceAdapters(
        trusted_safety_providers=("goplus",)
    )
    report = adapter.validate_composite_safety("mint", "solana", None, 1000)
    assert not report.passed
    assert "claim-specific composite safety provider allowlists are not configured" in report.reasons
