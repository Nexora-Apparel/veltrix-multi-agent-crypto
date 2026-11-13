from dataclasses import replace

import pytest

from veltrix.developer_concentration import HolderBalance, HolderSnapshot
from veltrix.providers.intelligence import TokenSafetyObservation
from veltrix.providers.solana_mint import (
    SolanaMintAuthoritySnapshot,
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
)
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceCollector

MINT = "So11111111111111111111111111111111111111112"
AUTHORITY = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FREEZER = "11111111111111111111111111111111"
NOW = 10_000


class FakeGoPlus:
    provider_id = "goplus"

    def __init__(self, observation=None, error=None):
        self.observation = observation
        self.error = error

    def fetch_safety_observation(self, chain, token_address):
        assert chain == "solana"
        assert token_address == MINT
        if self.error:
            raise self.error
        return self.observation


class FakeHolderProvider:
    provider_id = "solana-rpc-holder-inventory"

    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error

    def fetch_snapshot(self, mint):
        assert mint == MINT
        if self.error:
            raise self.error
        return self.snapshot


class FakeMintProvider:
    provider_id = "solana-finalized-mint-account"

    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error

    def fetch_snapshot(self, mint):
        assert mint == MINT
        if self.error:
            raise self.error
        return self.snapshot


def goplus_observation(**overrides):
    values = dict(
        provider="goplus",
        token_address=MINT,
        evidence_id="goplus-evidence-1",
        observed_at_ms=NOW,
        liquidity_locked=False,
        dev_hold_pct=3.0,
        bundled_supply_pct=2.0,
        creator_hold_pct=1.0,
    )
    values.update(overrides)
    return TokenSafetyObservation(**values)


def holder_snapshot(**overrides):
    values = dict(
        provider="solana-rpc-holder-inventory",
        chain="solana",
        token_address=MINT,
        observed_at_ms=NOW,
        evidence_id="holders-evidence-1",
        block_ref="finalized-slot:100",
        decimals=9,
        total_supply_raw=1000,
        holders=(
            HolderBalance(AUTHORITY, 600),
            HolderBalance(FREEZER, 200),
            HolderBalance("Vote111111111111111111111111111111111111111", 200),
        ),
        coverage_complete=True,
    )
    values.update(overrides)
    return HolderSnapshot(**values)


def mint_snapshot(**overrides):
    values = dict(
        mint=MINT,
        provider="solana-finalized-mint-account",
        evidence_id="mint-evidence-1",
        observed_at_ms=NOW,
        finalized_slot=100,
        token_program_id=TOKEN_PROGRAM_ID,
        mint_authority=AUTHORITY,
        freeze_authority=None,
        supply_raw=1000,
        decimals=9,
        extension_types=(),
        unknown_extension_ids=(),
        extension_tlv_structure_valid=True,
        account_data_sha256="a" * 64,
    )
    values.update(overrides)
    return SolanaMintAuthoritySnapshot(**values)


def collector(goplus=True, holders=True, mint=True, **kwargs):
    return SolanaRiskEvidenceCollector(
        FakeGoPlus(goplus_observation()) if goplus else None,
        FakeHolderProvider(holder_snapshot()) if holders else None,
        mint_provider=FakeMintProvider(mint_snapshot()) if mint else None,
        clock_ms=lambda: NOW,
        **kwargs,
    )


def test_collects_mint_holder_and_supplemental_goplus_evidence_without_safe_verdict():
    report = collector().collect(MINT)

    assert report.mint_evidence_id == "mint-evidence-1"
    assert report.mint_authority == AUTHORITY
    assert report.freeze_authority is None
    assert report.mint_supply_reconciled is True
    assert report.holder_coverage_complete is True
    assert report.top_holder_pct == 60.0
    assert report.top5_holder_pct == 100.0
    assert report.liquidity_locked is False
    assert report.risk_state == "REQUIRES_MORE_EVIDENCE"
    assert "liquidity_lock_complete_inventory" in report.missing_claims
    assert "developer_wallet_linkage" in report.missing_claims
    assert not hasattr(report, "is_safe")
    codes = {flag.code for flag in report.risk_flags}
    assert "mint_authority_active" in codes
    assert "top_holder_concentration_critical" in codes
    assert "goplus_reports_unlocked_liquidity" in codes


def test_active_freeze_authority_is_critical_and_known_extension_is_reviewed():
    token_2022 = mint_snapshot(
        token_program_id=TOKEN_2022_PROGRAM_ID,
        freeze_authority=FREEZER,
        extension_types=("permanent_delegate", "transfer_fee_config"),
    )
    risk = SolanaRiskEvidenceCollector(
        FakeGoPlus(goplus_observation()),
        FakeHolderProvider(holder_snapshot()),
        mint_provider=FakeMintProvider(token_2022),
        clock_ms=lambda: NOW,
    ).collect(MINT)

    assert risk.token_2022_extension_review_complete is True
    assert "token_2022_extension_review" not in risk.missing_claims
    flags = {flag.code: flag.severity for flag in risk.risk_flags}
    assert flags["freeze_authority_active"] == "critical"
    assert flags["token2022_permanent_delegate"] == "critical"
    assert flags["token2022_transfer_fee_config"] == "medium"


def test_unknown_token_2022_extension_remains_unreviewed_and_flagged():
    unknown = mint_snapshot(
        token_program_id=TOKEN_2022_PROGRAM_ID,
        extension_types=("unknown_65000",),
        unknown_extension_ids=(65000,),
    )
    report = SolanaRiskEvidenceCollector(
        None, None, mint_provider=FakeMintProvider(unknown), clock_ms=lambda: NOW
    ).collect(MINT)

    assert report.token_2022_extension_review_complete is False
    assert "token_2022_extension_review" in report.missing_claims
    assert "token2022_unknown_extension_ids" in {flag.code for flag in report.risk_flags}


def test_incomplete_holder_inventory_never_emits_concentration_percentages():
    partial = holder_snapshot(coverage_complete=False)
    report = SolanaRiskEvidenceCollector(
        None, FakeHolderProvider(partial),
        mint_provider=FakeMintProvider(mint_snapshot()),
        clock_ms=lambda: NOW,
    ).collect(MINT)

    assert report.holder_coverage_complete is False
    assert report.top_holder_pct is None
    assert "complete_holder_inventory" in report.missing_claims
    assert "holder_inventory_incomplete" in {flag.code for flag in report.risk_flags}


def test_mint_and_holder_supply_mismatch_is_critical_and_unreconciled():
    report = SolanaRiskEvidenceCollector(
        None,
        FakeHolderProvider(holder_snapshot(total_supply_raw=999)),
        mint_provider=FakeMintProvider(mint_snapshot()),
        clock_ms=lambda: NOW,
    ).collect(MINT)

    assert report.mint_supply_reconciled is False
    assert "mint_holder_supply_reconciliation" in report.missing_claims
    assert "mint_holder_supply_mismatch" in {flag.code for flag in report.risk_flags}


def test_stale_or_future_evidence_is_rejected_and_reported():
    stale = replace(holder_snapshot(), observed_at_ms=1)
    future = replace(mint_snapshot(), observed_at_ms=NOW + 1)
    report = SolanaRiskEvidenceCollector(
        None,
        FakeHolderProvider(stale),
        mint_provider=FakeMintProvider(future),
        clock_ms=lambda: NOW,
        max_age_ms=100,
    ).collect(MINT)

    assert "holders: ProviderError" in report.errors
    assert "mint_account: ProviderError" in report.errors
    assert report.top_holder_pct is None
    assert report.mint_evidence_id is None


def test_provider_errors_do_not_become_positive_safety_evidence():
    report = SolanaRiskEvidenceCollector(
        FakeGoPlus(error=RuntimeError("offline")),
        FakeHolderProvider(error=RuntimeError("rpc unavailable")),
        mint_provider=FakeMintProvider(error=RuntimeError("rpc unavailable")),
        clock_ms=lambda: NOW,
    ).collect(MINT)

    assert report.liquidity_locked is None
    assert report.holder_coverage_complete is False
    assert report.mint_evidence_id is None
    assert len(report.errors) == 3
    assert report.risk_state == "REQUIRES_MORE_EVIDENCE"


def test_missing_providers_are_explicit_unknown_claims():
    report = collector(goplus=False, holders=False, mint=False).collect(MINT)
    assert "goplus_token_security_observation" in report.missing_claims
    assert "complete_holder_inventory" in report.missing_claims
    assert "mint_account_state" in report.missing_claims
    assert report.risk_flags == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_age_ms": 0},
        {"max_age_ms": True},
    ],
)
def test_rejects_invalid_evidence_freshness_configuration(kwargs):
    with pytest.raises(ValueError):
        SolanaRiskEvidenceCollector(None, None, **kwargs)


def test_rejects_invalid_mint_before_provider_calls():
    providers = collector()
    with pytest.raises(ValueError):
        providers.collect("not-a-solana-mint")

def test_goplus_percentages_are_retained_as_supplemental_flags_only():
    observation = goplus_observation(
        dev_hold_pct=55.0,
        bundled_supply_pct=25.0,
        creator_hold_pct=30.0,
    )
    report = SolanaRiskEvidenceCollector(
        FakeGoPlus(observation),
        FakeHolderProvider(holder_snapshot()),
        mint_provider=FakeMintProvider(mint_snapshot()),
        clock_ms=lambda: NOW,
    ).collect(MINT)

    assert report.goplus_dev_hold_pct == 55.0
    assert report.goplus_bundled_supply_pct == 25.0
    assert report.goplus_creator_hold_pct == 30.0
    codes = {flag.code for flag in report.risk_flags}
    assert "goplus_reports_high_dev_holdings" in codes
    assert "goplus_reports_elevated_bundle_supply" in codes
    assert "goplus_reports_elevated_creator_holdings" in codes
    assert "bundled_supply" in report.missing_claims
    assert "developer_wallet_linkage" in report.missing_claims



def test_holder_percentages_require_mint_supply_and_decimals_reconciliation():
    report = SolanaRiskEvidenceCollector(
        None,
        FakeHolderProvider(holder_snapshot()),
        mint_provider=FakeMintProvider(mint_snapshot(supply_raw=999)),
        clock_ms=lambda: NOW,
    ).collect(MINT)

    assert report.holder_coverage_complete is True
    assert report.mint_supply_reconciled is False
    assert report.top_holder_pct is None
    assert report.top5_holder_pct is None
    assert report.top10_holder_pct is None
    assert "complete_holder_inventory" not in report.missing_claims
    assert "mint_holder_supply_reconciliation" in report.missing_claims
    codes = {flag.code for flag in report.risk_flags}
    assert "mint_holder_supply_mismatch" in codes
    assert "holder_inventory_incomplete" not in codes


def test_provider_exception_text_is_not_persisted_in_risk_report():
    report = SolanaRiskEvidenceCollector(
        FakeGoPlus(error=RuntimeError("request URL contains private-secret")),
        FakeHolderProvider(error=RuntimeError("RPC URL contains private-secret")),
        mint_provider=FakeMintProvider(error=RuntimeError("endpoint contains private-secret")),
        clock_ms=lambda: NOW,
    ).collect(MINT)

    assert len(report.errors) == 3
    assert all("private-secret" not in error for error in report.errors)
    assert report.errors == (
        "goplus: RuntimeError",
        "holders: RuntimeError",
        "mint_account: RuntimeError",
    )


def test_risk_report_rejects_concentration_without_reconciled_supply():
    report = collector().collect(MINT)
    with pytest.raises(ValueError, match="require complete, mint-reconciled"):
        replace(report, mint_supply_reconciled=False)



def test_mint_reconciliation_requires_both_supplies_and_matching_decimals():
    report = collector().collect(MINT)
    assert report.mint_supply_reconciled is True
    assert report.mint_supply_raw == report.total_supply_raw
    assert report.mint_decimals == report.holder_decimals

    with pytest.raises(ValueError, match="matching reported evidence"):
        replace(report, mint_supply_raw=None)
    with pytest.raises(ValueError, match="matching reported evidence"):
        replace(report, total_supply_raw=None)
    with pytest.raises(ValueError, match="matching reported evidence"):
        replace(report, holder_decimals=report.holder_decimals + 1)
    with pytest.raises(ValueError, match="matching reported evidence"):
        replace(report, mint_decimals=None)
