from dataclasses import replace

from veltrix.developer_concentration import HolderBalance, HolderSnapshot
from veltrix.providers.intelligence import TokenSafetyObservation
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore
from veltrix.solana_meme.sellability import (
    SolanaSellQuoteObservation, SolanaSellabilityEvaluator, WRAPPED_SOL_MINT,
)
from veltrix.solana_meme.sellability_store import SolanaSellabilityEvidenceStore
from veltrix.solana_meme.sellability_worker import SolanaSellabilityWorker

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class FakeQuoteProvider:
    provider_id = "fake-jupiter"

    def fetch_sell_quote(self, mint, input_amount_raw):
        return SolanaSellQuoteObservation(
            provider=self.provider_id, evidence_id=f"quote:{input_amount_raw}",
            mint=mint, output_mint=WRAPPED_SOL_MINT,
            input_amount_raw=input_amount_raw, quoted_output_raw=500,
            minimum_output_raw=490, route_plan_count=1,
            price_impact_bps=50.0, observed_at_ms=100_000,
            expires_at_ms=115_000,
        )


def risk_report(now=100_000, *, mint_evidence=True):
    return SolanaRiskEvidenceReport(
        mint=MINT, observed_at_ms=now, goplus_provider="goplus",
        goplus_evidence_id="gp:1", goplus_observed_at_ms=now,
        liquidity_locked=None, holder_provider="rpc",
        holder_evidence_id="holders:1", holder_block_ref="slot:1",
        holder_count=1, total_supply_raw=1_000_000_000,
        observed_holder_balance_raw=1_000_000_000,
        holder_coverage_complete=True, top_holder_pct=100.0,
        top5_holder_pct=100.0, top10_holder_pct=100.0,
        missing_claims=("executable_sellability",), errors=(),
        mint_evidence_id="mint:1" if mint_evidence else None,
        mint_finalized_slot=1 if mint_evidence else None,
    )


def setup(tmp_path, *, now=105_000):
    path = tmp_path / "sellability.sqlite3"
    launches = LaunchStore(path)
    risks = SolanaRiskEvidenceStore(path)
    store = SolanaSellabilityEvidenceStore(path)
    candidate = normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token", observed_at_ms=90_000,
    )
    launches.record_candidate(candidate)
    risks.record(risk_report())
    evaluator = SolanaSellabilityEvaluator(
        FakeQuoteProvider(), clock_ms=lambda: now,
    )
    worker = SolanaSellabilityWorker(
        launches, risks, evaluator, store, clock_ms=lambda: now,
    )
    return launches, risks, store, evaluator, worker


def test_worker_persists_route_observation_without_clearing_risk_claim(tmp_path):
    _, risks, store, _, worker = setup(tmp_path)
    result = worker.run_once()
    assert result.discovered == 1
    assert result.stored == 1
    assert result.route_observed == 1
    latest = store.latest(MINT)
    assert latest["state"] == "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN"
    assert latest["report"]["execution_proven"] is False
    assert "executable_sellability" in risks.latest(MINT)["missing_claims"]


def test_worker_skips_candidates_without_finalized_supply_evidence(tmp_path):
    path = tmp_path / "no-supply.sqlite3"
    launches = LaunchStore(path)
    risks = SolanaRiskEvidenceStore(path)
    store = SolanaSellabilityEvidenceStore(path)
    candidate = normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token", observed_at_ms=90_000,
    )
    launches.record_candidate(candidate)
    risks.record(risk_report(mint_evidence=False))
    evaluator = SolanaSellabilityEvaluator(FakeQuoteProvider(), clock_ms=lambda: 105_000)
    worker = SolanaSellabilityWorker(
        launches, risks, evaluator, store, clock_ms=lambda: 105_000,
    )
    result = worker.run_once()
    assert result.discovered == 0
    assert result.processed == 0
    assert store.count() == 0


def test_store_is_append_only_and_deduplicates_identical_evidence(tmp_path):
    _, _, store, evaluator, _ = setup(tmp_path)
    report = evaluator.evaluate(
        MINT, total_supply_raw=1_000_000_000,
        input_amount_raw=10_000_000, now_ms=105_000,
    )
    assert store.record(report) is True
    assert store.record(report) is False
    assert store.record(replace(report, observed_at_ms=106_000)) is True
    assert store.count(MINT) == 2
    assert len(store.history(MINT)) == 2


def test_worker_rejects_split_databases_and_invalid_limits(tmp_path):
    _, risks, store, evaluator, worker = setup(tmp_path)
    other = LaunchStore(tmp_path / "other.sqlite3")
    try:
        SolanaSellabilityWorker(other, risks, evaluator, store)
        assert False, "expected split database rejection"
    except ValueError:
        pass
    for invalid in (0, 21, -1, True):
        try:
            worker.run_once(limit=invalid)
            assert False, "expected invalid limit rejection"
        except ValueError:
            pass
