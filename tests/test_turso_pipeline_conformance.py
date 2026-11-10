"""Remote end-to-end one-shot pipeline conformance on disposable Turso only."""
from __future__ import annotations

import os
import time
import uuid
from urllib.parse import urlsplit

import pytest

URL = os.getenv("TURSO_CONFORMANCE_DATABASE_URL", "").strip()
TOKEN = os.getenv("TURSO_CONFORMANCE_AUTH_TOKEN", "").strip()
EXPECTED_HOST = "veltrix-h0-conformance-test-nexora-apparel.aws-eu-central-1.turso.io"
# Deterministic, non-canonical test mint; never use a production token mint.
MINT = "4wBqpZM9xaSheZzJSMawUKKwhdpChKbZ5eu5ky4Vigw"

pytestmark = pytest.mark.skipif(
    not URL or not TOKEN,
    reason="dedicated disposable Turso conformance credentials are required",
)


def _assert_disposable_target() -> None:
    parsed = urlsplit(URL)
    if (
        parsed.scheme != "libsql"
        or parsed.hostname != EXPECTED_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise RuntimeError(f"pipeline conformance is restricted to {EXPECTED_HOST}")


def test_remote_one_shot_pipeline_order_idempotency_and_paper_off_by_default(monkeypatch):
    _assert_disposable_target()
    monkeypatch.setenv("TURSO_AUTH_TOKEN", TOKEN)

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
    from veltrix.solana_meme.pipeline import OneShotSolanaPipeline
    from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceCollector
    from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore
    from veltrix.solana_meme.risk_worker import SolanaRiskEvidenceWorker

    class Secondary:
        name = "turso-pipeline-conformance"

        def fetch_candidates(self):
            candidate = normalize_candidate(
                {"chain": "solana", "mint": MINT},
                source=self.name,
                observed_at_ms=now,
            )
            return DexScreenerBatch(
                candidates=(candidate,),
                successful_sources=(self.name,),
                errors=(),
            )

    class NoNetworkProvider:
        def fetch_token_pairs(self, chain, token_addresses):
            return [
                MarketEvent(
                    chain="solana",
                    token_address=mint,
                    pair_address="remote-pipeline-conformance-pool",
                    symbol="CONF",
                    name="Conformance Token",
                    price_usd=0.001,
                    liquidity_usd=5_000,
                    volume_h1_usd=1_000,
                    price_change_m5_pct=1.0,
                    observed_at_ms=now,
                    pair_created_at_ms=now - 10_000,
                    source="remote-conformance",
                    fdv_usd=100_000,
                    market_cap_usd=90_000,
                    volume_m5_usd=100,
                    volume_h24_usd=5_000,
                    price_change_h1_pct=2.0,
                    price_change_h24_pct=3.0,
                    txns_m5_buys=10,
                    txns_m5_sells=5,
                    txns_h1_buys=100,
                    txns_h1_sells=50,
                    txns_h24_buys=500,
                    txns_h24_sells=250,
                )
                for mint in token_addresses
            ]

    now = int(time.time() * 1000)
    run_tag = uuid.uuid4().hex
    idempotency_key = f"turso-one-shot-pipeline-{run_tag}"

    launches = LaunchStore(URL)
    markets = MarketSnapshotStore(URL)
    risks = SolanaRiskEvidenceStore(URL)
    decisions = ConsensusStore(URL)
    orchestration = OrchestrationStore(URL)
    orchestration.initialize()

    market_data = SolanaMarketData(NoNetworkProvider(), clock_ms=lambda: now)
    discovery = SolanaDiscoveryEngine(
        launches, Secondary(), clock_ms=lambda: now
    )
    enrichment = CandidateEnrichmentWorker(
        launches, market_data, markets, clock_ms=lambda: now
    )
    refresh = MarketRefreshWorker(
        launches, market_data, markets, refresh_interval_ms=1,
        clock_ms=lambda: now,
    )
    risk = SolanaRiskEvidenceWorker(
        launches,
        markets,
        SolanaRiskEvidenceCollector(None, None, clock_ms=lambda: now),
        risks,
        clock_ms=lambda: now,
    )
    consensus = ConsensusWorker(
        launches, markets, risks, ConsensusEngine(), decisions,
        clock_ms=lambda: now,
    )
    pipeline = OneShotSolanaPipeline(
        orchestration, discovery, enrichment, refresh, risk, consensus,
    )

    try:
        first = pipeline.run_once(idempotency_key=idempotency_key)
        assert first.job["status"] == "SUCCEEDED"
        assert first.job["executed"] is True
        assert first.stages is not None
        assert first.stages.discovery.candidates_stored == 1
        assert first.stages.enrichment.enriched == 1
        assert first.stages.risk.recorded == 1
        assert first.stages.consensus.recorded == 1
        assert first.stages.paper is None
        assert pipeline.paper_enabled is False
        assert decisions.latest(MINT)["execution_authorized"] is False

        second = pipeline.run_once(idempotency_key=idempotency_key)
        assert second.job["executed"] is False
        assert second.stages is None
        assert launches.candidate_count() >= 1
        assert markets.count(MINT) >= 1
        assert risks.count(MINT) >= 1
        assert decisions.count(MINT) >= 1
    finally:
        # Remove only the fixed synthetic mint's rows from the exact disposable
        # database. Orchestration/audit records remain append-only evidence.
        with launches._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in (
                "solana_enrichment_jobs",
                "solana_market_refresh_jobs",
                "solana_risk_evidence_jobs",
                "solana_consensus_jobs",
            ):
                db.execute(f'DELETE FROM "{table}" WHERE mint=?', (MINT,))
            db.execute("DELETE FROM solana_consensus_decisions WHERE mint=?", (MINT,))
            db.execute("DELETE FROM solana_risk_evidence_reports WHERE mint=?", (MINT,))
            db.execute("DELETE FROM solana_market_snapshots WHERE mint=?", (MINT,))
            db.execute("DELETE FROM solana_launch_candidates WHERE mint=?", (MINT,))
            db.execute(
                "DELETE FROM launch_feed_events WHERE details LIKE ?",
                (f"%{MINT}%",),
            )
