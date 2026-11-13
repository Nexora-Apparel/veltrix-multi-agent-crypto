from dataclasses import fields

import pytest

from veltrix.developer_concentration import HolderBalance, HolderSnapshot
from veltrix.providers.intelligence import TokenSafetyObservation
from veltrix.providers.solana_mint import SolanaMintAuthoritySnapshot, TOKEN_PROGRAM_ID
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceCollector, SolanaRiskEvidenceReport
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore

MINT = "So11111111111111111111111111111111111111112"
AUTHORITY = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class GoPlus:
    provider_id = "goplus"

    def fetch_safety_observation(self, chain, token_address):
        return TokenSafetyObservation(
            provider="goplus", token_address=token_address,
            evidence_id="gp-1", observed_at_ms=1000,
            liquidity_locked=False, dev_hold_pct=2.0,
            bundled_supply_pct=1.0,
        )


class Holders:
    provider_id = "solana-rpc-holder-inventory"

    def fetch_snapshot(self, mint):
        return HolderSnapshot(
            provider=self.provider_id, chain="solana", token_address=mint,
            observed_at_ms=1000, evidence_id="holders-1", block_ref="slot:1",
            decimals=9, total_supply_raw=100,
            holders=(HolderBalance(AUTHORITY, 100),),
            coverage_complete=True,
        )


class Mint:
    provider_id = "solana-finalized-mint-account"

    def fetch_snapshot(self, mint):
        return SolanaMintAuthoritySnapshot(
            mint=mint, provider=self.provider_id, evidence_id="mint-1",
            observed_at_ms=1000, finalized_slot=1, token_program_id=TOKEN_PROGRAM_ID,
            mint_authority=None, freeze_authority=None, supply_raw=100, decimals=9,
            extension_types=(), unknown_extension_ids=(),
            extension_tlv_structure_valid=True, account_data_sha256="b" * 64,
        )


def make_report(observed):
    collector = SolanaRiskEvidenceCollector(
        GoPlus(), Holders(), mint_provider=Mint(), clock_ms=lambda: observed
    )
    return collector.collect(MINT, now_ms=observed)


def test_risk_store_is_append_only_and_deduplicates_identical_reports(tmp_path):
    store = SolanaRiskEvidenceStore(tmp_path / "risk.sqlite3")
    first = make_report(1000)
    assert store.record(first)
    assert not store.record(first)
    assert store.count(MINT) == 1

    second = make_report(2000)
    assert store.record(second)
    assert store.count(MINT) == 2


def test_risk_store_preserves_flags_and_missing_claims(tmp_path):
    store = SolanaRiskEvidenceStore(tmp_path / "risk.sqlite3")
    report = make_report(1000)
    store.record(report)

    latest = store.latest(MINT)
    assert latest["mint"] == MINT
    assert "liquidity_lock_complete_inventory" in latest["missing_claims"]
    assert isinstance(latest["risk_flags"], list)
    assert latest["risk_flags"][0]["code"]


def test_risk_store_history_is_newest_first_and_validates_limit(tmp_path):
    store = SolanaRiskEvidenceStore(tmp_path / "risk.sqlite3")
    store.record(make_report(1000))
    store.record(make_report(2000))
    assert [item["observed_at_ms"] for item in store.history(MINT)] == [2000, 1000]
    with pytest.raises(ValueError):
        store.history(MINT, limit=0)


def test_risk_store_requires_persistent_database():
    with pytest.raises(ValueError):
        SolanaRiskEvidenceStore(":memory:")



def test_risk_store_revalidates_forged_report_before_writing(tmp_path):
    store = SolanaRiskEvidenceStore(tmp_path / "risk.sqlite3")
    original = make_report(1000)
    forged = object.__new__(SolanaRiskEvidenceReport)
    for field in fields(SolanaRiskEvidenceReport):
        object.__setattr__(forged, field.name, getattr(original, field.name))
    object.__setattr__(forged, "observed_at_ms", True)

    with pytest.raises(ValueError, match="failed canonical validation"):
        store.record(forged)
    assert store.count(MINT) == 0


def test_risk_store_rejects_report_with_inconsistent_holder_percentages(tmp_path):
    store = SolanaRiskEvidenceStore(tmp_path / "risk.sqlite3")
    report = make_report(1000)
    forged = object.__new__(SolanaRiskEvidenceReport)
    for field in fields(SolanaRiskEvidenceReport):
        object.__setattr__(forged, field.name, getattr(report, field.name))
    object.__setattr__(forged, "mint_supply_reconciled", False)

    with pytest.raises(ValueError, match="failed canonical validation"):
        store.record(forged)
    assert store.count(MINT) == 0



def test_risk_store_rejects_forged_decimal_reconciliation_before_writing(tmp_path):
    store = SolanaRiskEvidenceStore(tmp_path / "risk.sqlite3")
    report = make_report(1000)
    forged = object.__new__(SolanaRiskEvidenceReport)
    for field in fields(SolanaRiskEvidenceReport):
        object.__setattr__(forged, field.name, getattr(report, field.name))
    object.__setattr__(forged, "holder_decimals", report.holder_decimals + 1)

    with pytest.raises(ValueError, match="failed canonical validation"):
        store.record(forged)
    assert store.count(MINT) == 0
