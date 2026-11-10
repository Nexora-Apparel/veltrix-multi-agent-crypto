from types import SimpleNamespace

import pytest

from veltrix.orchestration import OrchestrationStore
from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.consensus import ConsensusEngine
from veltrix.solana_meme.consensus_store import ConsensusStore
from veltrix.solana_meme.consensus_worker import ConsensusWorker
from veltrix.solana_meme.dexscreener import DexScreenerBatch
from veltrix.solana_meme.discovery import SolanaDiscoveryEngine
from veltrix.solana_meme.enrichment import CandidateEnrichmentWorker
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_refresh import MarketRefreshWorker
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.pipeline import OneShotSolanaPipeline, build_one_shot_pipeline
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceCollector
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore
from veltrix.solana_meme.risk_worker import SolanaRiskEvidenceWorker

MINT = "So11111111111111111111111111111111111111112"


class Secondary:
    name = "dexscreener"

    def fetch_candidates(self):
        candidate = normalize_candidate(
            {"chain": "solana", "mint": MINT},
            source="dexscreener-latest-profile",
            observed_at_ms=9_000,
        )
        return DexScreenerBatch(
            candidates=(candidate,),
            successful_sources=("dexscreener-latest-profile",),
            errors=(),
        )


class MarketProvider:
    def fetch_token_pairs(self, chain, token_addresses):
        return [
            MarketEvent(
                chain="solana",
                token_address=mint,
                pair_address="pool-1",
                symbol="MEME",
                name="Meme Token",
                price_usd=0.001,
                liquidity_usd=5_000,
                volume_h1_usd=1_200,
                price_change_m5_pct=1.2,
                observed_at_ms=9_000,
                pair_created_at_ms=8_000,
                source="dexscreener",
                fdv_usd=100_000,
                market_cap_usd=90_000,
                volume_m5_usd=100,
                volume_h24_usd=10_000,
                price_change_h1_pct=2.0,
                price_change_h24_pct=10.0,
                txns_m5_buys=10,
                txns_m5_sells=5,
                txns_h1_buys=100,
                txns_h1_sells=60,
                txns_h24_buys=900,
                txns_h24_sells=700,
            )
            for mint in token_addresses
        ]


def build_pipeline(tmp_path, *, paper_enabled=False):
    path = tmp_path / "pipeline.sqlite3"
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    risks = SolanaRiskEvidenceStore(path)
    decisions = ConsensusStore(path)
    orchestration = OrchestrationStore(str(path))
    orchestration.initialize()

    discovery = SolanaDiscoveryEngine(
        launches, Secondary(), clock_ms=lambda: 10_000
    )
    market_data = SolanaMarketData(MarketProvider(), clock_ms=lambda: 10_000)
    enrichment = CandidateEnrichmentWorker(
        launches, market_data, markets, clock_ms=lambda: 10_000
    )
    refresh = MarketRefreshWorker(
        launches, market_data, markets, clock_ms=lambda: 10_000
    )
    risk = SolanaRiskEvidenceWorker(
        launches,
        markets,
        SolanaRiskEvidenceCollector(None, None, clock_ms=lambda: 10_000),
        risks,
        clock_ms=lambda: 10_000,
    )
    consensus = ConsensusWorker(
        launches, markets, risks, ConsensusEngine(), decisions,
        clock_ms=lambda: 10_000,
    )
    pipeline = OneShotSolanaPipeline(
        orchestration, discovery, enrichment, refresh, risk, consensus,
        paper_enabled=paper_enabled,
    )
    return pipeline, launches, markets, risks, decisions


def test_one_shot_pipeline_runs_stages_in_contract_order(tmp_path):
    pipeline, launches, markets, risks, decisions = build_pipeline(tmp_path)

    result = pipeline.run_once(idempotency_key="solana-window-2026-11-10T17:00Z")

    assert result.job["status"] == "SUCCEEDED"
    assert result.job["executed"] is True
    assert result.stages is not None
    assert result.stages.discovery.candidates_stored == 1
    assert result.stages.enrichment.enriched == 1
    assert result.stages.market_refresh.processed == 0
    assert result.stages.risk.recorded == 1
    assert result.stages.consensus.recorded == 1
    assert launches.candidate_count() == 1
    assert markets.count(MINT) == 1
    assert risks.count(MINT) == 1
    assert decisions.count(MINT) == 1
    assert decisions.latest(MINT)["execution_authorized"] is False


def test_one_shot_pipeline_is_idempotent_for_same_window(tmp_path):
    pipeline, *_ = build_pipeline(tmp_path)
    key = "solana-window-2026-11-10T17:05Z"

    first = pipeline.run_once(idempotency_key=key)
    second = pipeline.run_once(idempotency_key=key)

    assert first.job["executed"] is True
    assert second.job["executed"] is False
    assert second.stages is None


def test_one_shot_pipeline_rejects_paper_stage_until_transaction_fenced(tmp_path):
    with pytest.raises(ValueError, match="transaction-scoped fencing"):
        build_pipeline(tmp_path, paper_enabled=True)


def test_one_shot_pipeline_rejects_missing_idempotency_key(tmp_path):
    pipeline, *_ = build_pipeline(tmp_path)
    with pytest.raises(ValueError, match="idempotency_key"):
        pipeline.run_once(idempotency_key=" ")


def test_production_builder_disables_pumpportal_supervisor_and_shares_database(
    tmp_path, monkeypatch
):
    from veltrix.solana_meme import (
        consensus_runtime, intelligence_worker, risk_runtime, worker,
    )

    database = str(tmp_path / "builder.sqlite3")
    launch_store = LaunchStore(database)
    seen = {}

    class Stage:
        def __init__(self):
            self.database_path = database

        def run_once(self, **kwargs):
            raise AssertionError("builder test must not execute workers")

    def fake_discovery(**kwargs):
        seen["include_primary_stream"] = kwargs["include_primary_stream"]
        return SimpleNamespace(store=launch_store)

    def fake_intelligence(**kwargs):
        return SimpleNamespace(enrichment=Stage(), refresh=Stage())

    def fake_risk(**kwargs):
        return SimpleNamespace(worker=Stage())

    def fake_consensus(**kwargs):
        return SimpleNamespace(worker=Stage())

    monkeypatch.setattr(worker, "build_engine", fake_discovery)
    monkeypatch.setattr(intelligence_worker, "build_runtime", fake_intelligence)
    monkeypatch.setattr(risk_runtime, "build_runtime", fake_risk)
    monkeypatch.setattr(consensus_runtime, "build_runtime", fake_consensus)

    pipeline = build_one_shot_pipeline(database_path=database)

    assert isinstance(pipeline, OneShotSolanaPipeline)
    assert seen["include_primary_stream"] is False
    assert pipeline.orchestration.database == database
