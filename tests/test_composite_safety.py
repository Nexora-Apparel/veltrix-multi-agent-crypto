from veltrix.composite_safety import (
    CompositeTokenSafetyValidator, SafetyEvidenceBundle,
)
from veltrix.launch_bundles import LaunchBundleReport
from veltrix.safety import SafetyEvidence


NOW = 2_000_000
TOKEN = "MintABC"


def evidence(provider, **overrides):
    values = dict(
        token_address=TOKEN, provider=provider, evidence_id=f"id-{provider}",
        observed_at_ms=NOW - 100, liquidity_locked=None,
        dev_hold_pct=None, bundled_supply_pct=None,
        claim=None, chain="solana", coverage_complete=True,
        source_ref=f"https://evidence.invalid/{provider}", method="complete_onchain_index",
        independence_group=provider,
    )
    values.update(overrides)
    if values.get("liquidity_locked") is True:
        values["claim"] = "liquidity_lock"
    elif values.get("dev_hold_pct") is not None:
        values["claim"] = "developer_holdings"
    return SafetyEvidence(**values)


def valid_bundle(**overrides):
    report_values = dict(
        chain="solana", token_address=TOKEN, provider="bundle-indexer",
        launch_at_ms=NOW - 10_000, window_end_ms=NOW - 1_000,
        coverage_complete=True, bundled_supply_pct=0.0,
        shared_funder_clusters=0, participating_wallets=0,
        accepted_transactions=10, rejected=(), notes=(),
        evidence_id="bundle-evidence", observed_at_ms=NOW - 100,
        scanned_through_ms=NOW - 900, supply_source="bundle-indexer",
        supply_block_ref="slot:123", supply_observed_at_ms=NOW - 100,
        pages_scanned=2, funder_providers=("funder-indexer",),
        provenance=("scan:complete",),
    )
    report_values.update(overrides)
    return SafetyEvidenceBundle(
        liquidity=evidence("lock-a", liquidity_locked=True),
        developer_holdings=evidence("dev-b", dev_hold_pct=4.5),
        launch_bundle=LaunchBundleReport(**report_values),
    )


def validator():
    return CompositeTokenSafetyValidator(
        trusted_liquidity_providers=["lock-a"],
        trusted_developer_providers=["dev-b"],
        trusted_bundle_providers=["bundle-indexer"],
        trusted_funding_providers=["funder-indexer"],
        max_age_ms=300_000,
    )


def test_composite_gate_passes_only_with_three_distinct_trusted_sources():
    report = validator().validate(TOKEN, valid_bundle(), NOW, chain="solana")
    assert report.passed
    assert set(report.checks) == {
        "liquidity_locked", "dev_holdings_below_5_percent", "bundled_supply_zero"
    }


def test_missing_composite_evidence_fails_closed():
    report = validator().validate(TOKEN, None, NOW, chain="solana")
    assert not report.passed
    assert "composite safety evidence is missing" in report.reasons


def test_untrusted_provider_or_shared_provider_fails():
    shared = valid_bundle()
    shared = SafetyEvidenceBundle(
        liquidity=evidence("lock-a", liquidity_locked=True),
        developer_holdings=evidence("lock-a", dev_hold_pct=1.0),
        launch_bundle=shared.launch_bundle,
    )
    report = validator().validate(TOKEN, shared, NOW, chain="solana")
    assert not report.passed
    assert "developer holdings: provider is not trusted" in report.reasons
    assert "liquidity and developer claims must use distinct providers" in report.reasons


def test_bundle_must_be_complete_zero_and_rejection_free():
    for changes in (
        {"coverage_complete": False},
        {"bundled_supply_pct": None},
        {"bundled_supply_pct": 0.01},
        {"rejected": ("one transaction failed validation",)},
        {"shared_funder_clusters": 1},
    ):
        report = validator().validate(TOKEN, valid_bundle(**changes), NOW, chain="solana")
        assert not report.passed


def test_wrong_token_stale_or_future_evidence_fails():
    wrong_token = SafetyEvidenceBundle(
        liquidity=evidence("lock-a", liquidity_locked=True, token_address="OtherMint"),
        developer_holdings=evidence("dev-b", dev_hold_pct=1),
        launch_bundle=valid_bundle().launch_bundle,
    )
    assert not validator().validate(TOKEN, wrong_token, NOW, chain="solana").passed

    stale = SafetyEvidenceBundle(
        liquidity=evidence("lock-a", liquidity_locked=True, observed_at_ms=NOW - 400_000),
        developer_holdings=evidence("dev-b", dev_hold_pct=1),
        launch_bundle=valid_bundle().launch_bundle,
    )
    assert not validator().validate(TOKEN, stale, NOW, chain="solana").passed

    future_report = valid_bundle(window_end_ms=NOW + 1)
    assert not validator().validate(TOKEN, future_report, NOW, chain="solana").passed


def test_malformed_claim_objects_fail_without_crashing():
    malformed = SafetyEvidenceBundle(
        liquidity=None, developer_holdings=evidence("dev-b", dev_hold_pct=1),
        launch_bundle=valid_bundle().launch_bundle,
    )
    report = validator().validate(TOKEN, malformed, NOW, chain="solana")
    assert not report.passed
    assert "liquidity: typed evidence is missing" in report.reasons



def test_bundle_chain_must_match_requested_token_chain():
    report = validator().validate(TOKEN, valid_bundle(), NOW, chain="base")
    assert not report.passed
    assert "launch bundle chain mismatch" in report.reasons


def test_malformed_launch_provenance_fails_closed_without_crashing():
    report = valid_bundle(
        accepted_transactions="not-an-int",
        scanned_through_ms="unknown",
    )
    result = validator().validate(TOKEN, report, NOW, chain="solana")
    assert not result.passed
    assert "launch transaction count is invalid" in result.reasons
    assert "launch indexer did not prove coverage through window end" in result.reasons
