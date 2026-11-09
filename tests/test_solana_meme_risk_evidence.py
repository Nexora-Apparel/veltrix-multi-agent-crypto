from veltrix.developer_concentration import HolderBalance, HolderSnapshot
from veltrix.providers.intelligence import TokenSafetyObservation
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


def collector(goplus=None, holder=None):
    return SolanaRiskEvidenceCollector(
        goplus or FakeGoPlus(safety()),
        holder or FakeHolders(holders()),
        max_age_ms=60_000,
        clock_ms=lambda: 10_000,
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
