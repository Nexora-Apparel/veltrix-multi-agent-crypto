from dataclasses import dataclass

from veltrix.composite_safety import CompositeTokenSafetyValidator
from veltrix.launch_bundles import LaunchBundleAnalyzer, LaunchBuyEvidence
from veltrix.launch_intelligence import (
    LaunchCoverage,
    LaunchIndexSnapshot,
    LaunchIntelligenceService,
)
from veltrix.safety import SafetyEvidence


CHAIN = "solana"
TOKEN = "MintABC"
LAUNCH = 1_000_000
WINDOW = 120_000
NOW = LAUNCH + WINDOW


@dataclass
class FakeLaunchIndexer:
    snapshot: LaunchIndexSnapshot
    provider_id: str = "launch-indexer"
    supported_chains: frozenset = frozenset({"solana", "base", "bsc"})

    def fetch_launch_snapshot(self, chain, token_address, launch_window_ms):
        assert chain == CHAIN
        assert token_address == TOKEN
        assert launch_window_ms == WINDOW
        return self.snapshot


def snapshot(*, pages_done=2, source_complete=True, observations=()):
    coverage = LaunchCoverage(
        provider="launch-indexer",
        chain=CHAIN,
        token_address=TOKEN,
        requested_start_ms=LAUNCH,
        requested_end_ms=LAUNCH + WINDOW,
        indexed_through_ms=LAUNCH + WINDOW,
        pages_expected=2,
        pages_completed=pages_done,
        source_complete=source_complete,
        evidence_id="coverage-001",
    )
    return LaunchIndexSnapshot(
        provider="launch-indexer",
        chain=CHAIN,
        token_address=TOKEN,
        launch_at_ms=LAUNCH,
        total_supply=1_000_000,
        supply_decimals=6,
        observations=tuple(observations),
        coverage=coverage,
        observed_at_ms=NOW,
    )


def safety_evidence(provider, *, liquidity=None, dev=None):
    return SafetyEvidence(
        token_address=TOKEN,
        provider=provider,
        evidence_id=f"{provider}-evidence",
        observed_at_ms=NOW,
        liquidity_locked=liquidity,
        dev_hold_pct=dev,
        bundled_supply_pct=None,
    )


def service(indexer):
    validator = CompositeTokenSafetyValidator(
        trusted_liquidity_providers=["locker"],
        trusted_developer_providers=["holder-indexer"],
        trusted_bundle_providers=["launch-indexer"],
        max_age_ms=300_000,
    )
    return LaunchIntelligenceService(
        provider=indexer,
        analyzer=LaunchBundleAnalyzer(["launch-indexer"], launch_window_ms=WINDOW),
        composite_validator=validator,
        trusted_launch_providers=["launch-indexer"],
        launch_window_ms=WINDOW,
    )


def evaluate(indexer):
    return service(indexer).evaluate(
        chain=CHAIN,
        token_address=TOKEN,
        liquidity_evidence=safety_evidence("locker", liquidity=True),
        developer_evidence=safety_evidence("holder-indexer", dev=2.5),
        now_ms=NOW,
    )


def test_complete_empty_launch_scan_is_submitted_and_passes_gate():
    result = evaluate(FakeLaunchIndexer(snapshot()))
    assert result.bundle_report.coverage_complete is True
    assert result.bundle_report.bundled_supply_pct == 0
    assert result.safety_report.passed is True
    assert result.safety_report.composite_verified is True


def test_incomplete_pagination_withholds_bundle_percentage_and_fails_gate():
    result = evaluate(FakeLaunchIndexer(snapshot(pages_done=1)))
    assert result.bundle_report.coverage_complete is False
    assert result.bundle_report.bundled_supply_pct is None
    assert result.safety_report.passed is False


def test_source_cannot_claim_complete_when_source_flag_is_false():
    result = evaluate(FakeLaunchIndexer(snapshot(source_complete=False)))
    assert result.bundle_report.bundled_supply_pct is None
    assert result.safety_report.composite_verified is False


def test_shared_funder_supply_is_counted_and_composite_gate_rejects_it():
    buys = (
        LaunchBuyEvidence("launch-indexer", CHAIN, TOKEN, "wallet-a", "funder",
                          "sig-a", 1000, LAUNCH + 1),
        LaunchBuyEvidence("launch-indexer", CHAIN, TOKEN, "wallet-b", "funder",
                          "sig-b", 2000, LAUNCH + 2),
    )
    result = evaluate(FakeLaunchIndexer(snapshot(observations=buys)))
    assert result.bundle_report.bundled_supply_pct == 0.3
    assert result.bundle_report.shared_funder_clusters == 1
    assert result.safety_report.composite_verified is False


def test_untrusted_indexer_is_not_used_as_safety_evidence():
    indexer = FakeLaunchIndexer(snapshot(), provider_id="unknown-indexer")
    result = evaluate(indexer)
    assert result.bundle_report.bundled_supply_pct is None
    assert result.safety_report.composite_verified is False
    assert result.provider_error == "launch provider is untrusted or chain unsupported"


def test_wrong_token_snapshot_cannot_pass_coverage():
    bad = snapshot()
    bad = LaunchIndexSnapshot(
        provider=bad.provider, chain=bad.chain, token_address="OtherMint",
        launch_at_ms=bad.launch_at_ms, total_supply=bad.total_supply,
        supply_decimals=bad.supply_decimals, observations=bad.observations,
        coverage=bad.coverage, observed_at_ms=bad.observed_at_ms,
    )
    result = evaluate(FakeLaunchIndexer(bad))
    assert result.bundle_report.bundled_supply_pct is None
    assert result.safety_report.composite_verified is False
