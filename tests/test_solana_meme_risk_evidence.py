from veltrix.developer_concentration import HolderBalance, HolderSnapshot
from veltrix.providers.intelligence import TokenSafetyObservation
from veltrix.providers.solana_mint import (
    SolanaMintAuthoritySnapshot, SolanaMintAuthorityProvider, TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
)
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceCollector

MINT = "So11111111111111111111111111111111111111112"


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


class FakeMint:
    provider_id = SolanaMintAuthorityProvider.provider_id

    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error

    def fetch_snapshot(self, mint):
        assert mint == MINT
        if self.error:
            raise self.error
        return self.snapshot


class FakeHolders:
    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error

    def fetch_snapshot(self, mint):
        assert mint == MINT
        if self.error:
            raise self.error
        return self.snapshot


def safety():
    return TokenSafetyObservation(
        provider="goplus", token_address=MINT, evidence_id="goplus:abc",
        observed_at_ms=10_000, liquidity_locked=None,
        dev_hold_pct=None, bundled_supply_pct=None,
    )


def holders(*, complete=True, total=1000):
    return HolderSnapshot(
        provider="solana-rpc-holder-inventory", chain="solana",
        token_address=MINT, observed_at_ms=10_000, evidence_id="holders:abc",
        block_ref="finalized-slot:123", decimals=6, total_supply_raw=total,
        holders=(
            HolderBalance("wallet-a", 500),
            HolderBalance("wallet-b", 300),
            HolderBalance("wallet-c", 200),
        ),
        coverage_complete=complete,
    )


def collector(goplus=None, holder=None, mint_provider=None, max_age_ms=60_000):
    return SolanaRiskEvidenceCollector(
        goplus or FakeGoPlus(safety()),
        holder or FakeHolders(holders()),
        mint_provider=mint_provider,
        max_age_ms=max_age_ms,
        clock_ms=lambda: 10_000,
    )


def mint_snapshot(*, authority=None, freeze=None, token_program=TOKEN_PROGRAM_ID,
                  extensions=(), observed=10_000):
    return SolanaMintAuthoritySnapshot(
        mint=MINT, provider=SolanaMintAuthorityProvider.provider_id,
        evidence_id="mint:abc", observed_at_ms=observed, finalized_slot=123,
        token_program_id=token_program, mint_authority=authority,
        freeze_authority=freeze, supply_raw=1000, decimals=6,
        extension_types=extensions, unknown_extension_ids=(),
        extension_tlv_structure_valid=True, account_data_sha256="a" * 64,
    )


def test_complete_holder_inventory_produces_concentration_only_not_safety_verdict():
    report = collector().collect(MINT)
    assert report.holder_coverage_complete is True
    assert report.top_holder_pct == 50.0
    assert report.top5_holder_pct == 100.0
    assert report.top10_holder_pct == 100.0
    assert report.risk_state == "REQUIRES_MORE_EVIDENCE"
    assert "developer_wallet_linkage" in report.missing_claims
    assert "executable_sellability" in report.missing_claims


def test_incomplete_or_unreconciled_holder_snapshot_never_emits_percentages():
    report = collector(holder=FakeHolders(holders(complete=False))).collect(MINT)
    assert report.holder_coverage_complete is False
    assert report.top_holder_pct is None
    assert "reconciled_complete_holder_inventory" in report.missing_claims

    underreported = holders(total=1200)
    report2 = collector(holder=FakeHolders(underreported)).collect(MINT)
    assert report2.holder_coverage_complete is False
    assert report2.top5_holder_pct is None


def test_provider_failures_are_separate_and_fail_closed():
    report = collector(
        goplus=FakeGoPlus(error=RuntimeError("offline")),
        holder=FakeHolders(error=RuntimeError("rpc offline")),
    ).collect(MINT)
    assert report.liquidity_locked is None
    assert report.holder_coverage_complete is False
    assert len(report.errors) == 2
    assert report.risk_state == "REQUIRES_MORE_EVIDENCE"


def test_stale_provider_evidence_is_not_used():
    old = TokenSafetyObservation(
        provider="goplus", token_address=MINT, evidence_id="old",
        observed_at_ms=1, liquidity_locked=True,
        dev_hold_pct=None, bundled_supply_pct=None,
    )
    collector_with_short_window = SolanaRiskEvidenceCollector(
        FakeGoPlus(old), FakeHolders(holders()),
        max_age_ms=5_000, clock_ms=lambda: 10_000,
    )
    report = collector_with_short_window.collect(MINT)
    assert report.liquidity_locked is None
    assert any("stale or future-dated" in item for item in report.errors)


def test_invalid_mint_is_rejected_before_provider_calls():
    class MustNotCall:
        provider_id = "none"
        def fetch_safety_observation(self, *args):
            raise AssertionError("must not call")
        def fetch_snapshot(self, *args):
            raise AssertionError("must not call")
    import pytest
    with pytest.raises(ValueError):
        SolanaRiskEvidenceCollector(MustNotCall(), MustNotCall()).collect("bad")



def test_finalized_mint_snapshot_resolves_authority_claims_for_classic_spl_token():
    report = collector(mint_provider=FakeMint(mint_snapshot())).collect(MINT)
    assert report.mint_evidence_id == "mint:abc"
    assert report.mint_finalized_slot == 123
    assert report.token_program_id == TOKEN_PROGRAM_ID
    assert report.mint_authority is None
    assert report.freeze_authority is None
    assert report.token_2022_extension_review_complete is True
    assert report.mint_supply_raw == 1000
    assert report.mint_decimals == 6
    assert report.mint_supply_reconciled is True
    for claim in ("mint_account_state", "mint_authority", "freeze_authority",
                  "token_2022_extension_review"):
        assert claim not in report.missing_claims


def test_active_mint_authorities_are_recorded_as_evidence_not_hidden():
    authority = "11111111111111111111111111111111"
    report = collector(
        mint_provider=FakeMint(mint_snapshot(authority=authority, freeze=authority))
    ).collect(MINT)
    assert report.mint_authority == authority
    assert report.freeze_authority == authority


def test_token_2022_extensions_remain_unreviewed_even_when_tlv_structure_is_valid():
    report = collector(
        mint_provider=FakeMint(mint_snapshot(
            token_program=TOKEN_2022_PROGRAM_ID,
            extensions=("permanent_delegate",),
        ))
    ).collect(MINT)
    assert report.token_2022_extension_types == ("permanent_delegate",)
    assert report.token_2022_extension_review_complete is False
    assert "token_2022_extension_review" in report.missing_claims


def test_stale_mint_snapshot_is_ignored_and_keeps_authority_claims_unknown():
    report = collector(
        mint_provider=FakeMint(mint_snapshot(observed=1)), max_age_ms=5_000,
    ).collect(MINT)
    assert report.mint_evidence_id is None
    assert "mint_authority" in report.missing_claims
    assert any("mint_account" in item for item in report.errors)


def test_mint_supply_or_decimals_mismatch_is_not_reconciled():
    report = collector(
        holder=FakeHolders(holders(total=999)),
        mint_provider=FakeMint(mint_snapshot()),
    ).collect(MINT)
    assert report.mint_supply_reconciled is False
    assert any("supply or decimals disagree" in item for item in report.errors)
