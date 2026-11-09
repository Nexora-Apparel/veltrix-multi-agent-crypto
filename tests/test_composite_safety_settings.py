from types import SimpleNamespace

from veltrix.composite_safety import SafetyEvidenceBundle
from veltrix.launch_bundles import LaunchBundleReport
from veltrix.providers.intelligence import VerifiedIntelligenceAdapters
from veltrix.safety import SafetyEvidence


def make_settings():
    return SimpleNamespace(
        trusted_wallet_providers=(),
        trusted_safety_providers=(),
        trusted_liquidity_providers=("lock-provider",),
        trusted_developer_providers=("dev-provider",),
        trusted_bundle_providers=("bundle-provider",),
        trusted_funding_providers=("funder-provider",),
        wallet_evidence_max_age_ms=300_000,
        safety_evidence_max_age_ms=300_000,
    )


def make_bundle(now):
    return SafetyEvidenceBundle(
        liquidity=SafetyEvidence(
            "MintABC", "lock-provider", "lock-1", now - 100,
            True, None, None,
        ),
        developer_holdings=SafetyEvidence(
            "MintABC", "dev-provider", "dev-1", now - 100,
            None, 4.0, None,
        ),
        launch_bundle=LaunchBundleReport(
            chain="solana", token_address="MintABC", provider="bundle-provider",
            launch_at_ms=now - 10_000, window_end_ms=now - 1_000,
            coverage_complete=True, bundled_supply_pct=0.0,
            shared_funder_clusters=0, participating_wallets=0,
            accepted_transactions=4, rejected=(), notes=(),
            evidence_id="bundle-evidence", observed_at_ms=now - 100,
            scanned_through_ms=now - 900, supply_source="bundle-provider",
            supply_block_ref="slot:123", supply_observed_at_ms=now - 100,
            pages_scanned=2, funder_providers=("funder-provider",),
            provenance=("indexed-range:1-2",),
        ),
    )


def test_from_settings_installs_composite_safety_validator():
    now = 2_000_000
    adapters = VerifiedIntelligenceAdapters.from_settings(make_settings())
    report = adapters.validate_composite_safety(
        "MintABC", "solana", make_bundle(now), now
    )
    assert report.passed
    assert report.composite_verified


def test_from_settings_does_not_promote_generic_provider_allowlists():
    settings = make_settings()
    settings.trusted_liquidity_providers = ()
    settings.trusted_developer_providers = ()
    settings.trusted_bundle_providers = ()
    adapters = VerifiedIntelligenceAdapters.from_settings(settings)
    report = adapters.validate_composite_safety(
        "MintABC", "solana", make_bundle(2_000_000), 2_000_000
    )
    assert not report.passed
    assert not report.composite_verified
