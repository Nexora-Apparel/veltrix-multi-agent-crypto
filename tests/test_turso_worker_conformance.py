"""Live Turso conformance for transaction-fenced Solana workers.

Runs only against the exact disposable H0 conformance database. Never point
these credentials at production or the paper ledger.
"""
from __future__ import annotations

import os
import time
import uuid
from urllib.parse import urlsplit

import pytest

URL = os.getenv("TURSO_CONFORMANCE_DATABASE_URL", "").strip()
TOKEN = os.getenv("TURSO_CONFORMANCE_AUTH_TOKEN", "").strip()
EXPECTED_HOST = "veltrix-h0-conformance-test-nexora-apparel.aws-eu-central-1.turso.io"

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
        raise RuntimeError(f"worker conformance is restricted to {EXPECTED_HOST}")


def test_all_solana_workers_support_remote_transaction_scoped_fencing(monkeypatch):
    _assert_disposable_target()
    monkeypatch.setenv("TURSO_AUTH_TOKEN", TOKEN)

    from veltrix.ledger import PaperBroker
    from veltrix.orchestration import OrchestrationStore, RunContext
    from veltrix.providers.base import MarketEvent
    from veltrix.solana_meme.candidates import normalize_candidate
    from veltrix.solana_meme.consensus import ConsensusEngine, MarketEvidence, RiskEvidence
    from veltrix.solana_meme.consensus_store import ConsensusStore
    from veltrix.solana_meme.consensus_worker import ConsensusWorker
    from veltrix.solana_meme.enrichment import CandidateEnrichmentWorker
    from veltrix.solana_meme.launch_stream import LaunchStore
    from veltrix.solana_meme.market_data import SolanaMarketData, SolanaMarketSnapshot
    from veltrix.solana_meme.market_refresh import MarketRefreshWorker
    from veltrix.solana_meme.market_store import MarketSnapshotStore
    from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceCollector
    from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore
    from veltrix.solana_meme.risk_worker import SolanaRiskEvidenceWorker

    class NoNetworkProvider:
        def fetch_token_pairs(self, chain, token_addresses):
            return [
                MarketEvent(
                    chain="solana", token_address=mint,
                    pair_address="conformance-pool", symbol="CONF",
                    name="Conformance Token", price_usd=0.001,
                    liquidity_usd=5_000, volume_h1_usd=1_000,
                    price_change_m5_pct=1.0, observed_at_ms=1,
                    pair_created_at_ms=1, source="conformance",
                )
                for mint in token_addresses
            ]

    now = int(time.time() * 1000)
    mint = "So11111111111111111111111111111111111111112"
    run_tag = uuid.uuid4().hex
    owner = f"turso-worker-{run_tag}"
    lease_name = f"turso-worker-conformance-{run_tag}"
    decision_id = f"paper-conformance-{run_tag}"

    launches = LaunchStore(URL)
    markets = MarketSnapshotStore(URL)
    risks = SolanaRiskEvidenceStore(URL)
    decisions = ConsensusStore(URL)
    broker = PaperBroker(URL, starting_balance=500.0)
    orchestration = OrchestrationStore(URL)
    orchestration.initialize()

    market_data = SolanaMarketData(NoNetworkProvider(), clock_ms=lambda: now)
    enrichment = CandidateEnrichmentWorker(
        launches, market_data, markets, clock_ms=lambda: now
    )
    refresh = MarketRefreshWorker(
        launches, market_data, markets, refresh_interval_ms=1,
        clock_ms=lambda: now,
    )
    risk = SolanaRiskEvidenceWorker(
        launches, markets,
        SolanaRiskEvidenceCollector(None, None, clock_ms=lambda: now),
        risks, clock_ms=lambda: now,
    )
    consensus = ConsensusWorker(
        launches, markets, risks, ConsensusEngine(), decisions,
        clock_ms=lambda: now,
    )

    token = orchestration.acquire_lease(
        owner, now_ms=now, ttl_ms=120_000, lease_name=lease_name
    )
    context = RunContext(
        run_tag, run_tag, 1, time.monotonic() + 30, 100,
        time.monotonic, token,
        lambda: orchestration.assert_fence(owner, token, lease_name=lease_name),
        owner_id=owner,
        lease_name=lease_name,
        _assert_fence_in_transaction=lambda db: orchestration.assert_fence_in_transaction(
            db, owner, token, lease_name=lease_name
        ),
    )

    try:
        # The target is disposable, but isolate this run from leftovers after
        # an interrupted prior dispatch using the same fixed conformance mint.
        with launches._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            context.assert_fence_in_transaction(db)
            for table in (
                "solana_enrichment_jobs",
                "solana_market_refresh_jobs",
                "solana_risk_evidence_jobs",
                "solana_consensus_jobs",
            ):
                db.execute(f'DELETE FROM "{table}" WHERE mint=?', (mint,))
            db.execute("DELETE FROM solana_consensus_decisions WHERE mint=?", (mint,))
            db.execute("DELETE FROM solana_risk_evidence_reports WHERE mint=?", (mint,))
            db.execute("DELETE FROM solana_market_snapshots WHERE mint=?", (mint,))
            db.execute("DELETE FROM solana_launch_candidates WHERE mint=?", (mint,))
            db.execute("DELETE FROM launch_feed_events WHERE details LIKE ?", (f"%{mint}%",))
            db.execute("DELETE FROM paper_consensus_decisions WHERE decision_id=?", (decision_id,))
            db.execute("DELETE FROM events WHERE payload LIKE ?", (f"%{decision_id}%",))

        candidate = normalize_candidate(
            {"chain": "solana", "mint": mint},
            source="turso-worker-conformance",
            observed_at_ms=now,
        )
        with launches._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            context.assert_fence_in_transaction(db)
            launches.record_candidate_in_transaction(candidate, db)

        # Exercise remote append-only market evidence persistence.
        snapshot = SolanaMarketSnapshot(
            mint=mint,
            selected_pair_address="conformance-pool",
            symbol="CONF",
            name="Conformance Token",
            price_usd=0.001,
            selected_pool_liquidity_usd=5_000,
            selected_pool_volume_m5_usd=100,
            selected_pool_volume_h1_usd=1_000,
            selected_pool_volume_h24_usd=5_000,
            price_change_m5_pct=1.0,
            price_change_h1_pct=2.0,
            price_change_h24_pct=3.0,
            txns_m5_buys=10,
            txns_m5_sells=5,
            txns_h1_buys=100,
            txns_h1_sells=50,
            txns_h24_buys=500,
            txns_h24_sells=250,
            fdv_usd=100_000,
            market_cap_usd=90_000,
            dex_id="conformance",
            quote_token_symbol="SOL",
            pool_token_side="base",
            pair_count=1,
            retrieved_at_ms=now - 1_000,
            response_received_at_ms=now - 1_000,
            pair_created_at_ms=now - 10_000,
            quality_flags=(),
        )
        markets.record(snapshot)

        report = SolanaRiskEvidenceCollector(None, None, clock_ms=lambda: now).collect(
            mint, now_ms=now
        )
        risks.record(report)
        market_row = markets.latest(mint)
        risk_payload = risks.latest(mint)
        decision = ConsensusEngine().evaluate(
            MarketEvidence.from_store_row(market_row),
            RiskEvidence.from_store_payload(risk_payload),
            now_ms=now,
        )
        decisions.record(decision)

        # Each worker performs a fenced claim and a fenced durable failure/retry
        # mutation on its own transaction connection.
        assert enrichment._discover_jobs(now, context) == 1
        assert enrichment._claim(now, 1, context) == [mint]
        enrichment._fail(mint, RuntimeError("conformance retry"), now + 1, context)
        assert enrichment.status()["retry"] == 1

        assert refresh._discover_jobs(now, context) == 1
        assert refresh._claim(now, 1, context) == [mint]
        refresh._fail(mint, RuntimeError("conformance retry"), now + 1, context)
        assert refresh.status()["retry"] == 1

        assert risk._discover_jobs(now, context) == 1
        assert risk._claim(now, 1, context) == [mint]
        risk._fail(mint, RuntimeError("conformance retry"), now + 1, context)
        assert risk.status()["retry"] == 1

        assert consensus._discover_jobs(now, context) == 1
        assert consensus._claim(now, 1, context) == [mint]
        consensus._fail(mint, RuntimeError("conformance retry"), now + 1, context)
        assert consensus.status()["retry"] == 1

        # Exercise caller-owned paper-ledger transaction and fence assertion.
        with broker._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            context.assert_fence_in_transaction(db)
            outcome = broker.record_consensus_outcome(
                decision_id, mint, "ABSTAIN", "NOT_ELIGIBLE",
                "remote fenced conformance",
                created_at_ms=now, processed_at_ms=now, db=db,
            )
            assert outcome["status"] == "NOT_ELIGIBLE"

        # Confirm all new queue tables and claim-identity columns are present.
        for table in (
            "solana_enrichment_jobs",
            "solana_market_refresh_jobs",
            "solana_risk_evidence_jobs",
            "solana_consensus_jobs",
        ):
            with launches._connect() as db:
                columns = {
                    row["name"] for row in db.execute(
                        f'PRAGMA table_info("{table}")'
                    ).fetchall()
                }
            assert {"claimed_by_owner_id", "claim_fencing_token"} <= columns
    finally:
        orchestration.release_lease(owner, token, lease_name=lease_name)
        with launches._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in (
                "solana_enrichment_jobs",
                "solana_market_refresh_jobs",
                "solana_risk_evidence_jobs",
                "solana_consensus_jobs",
            ):
                db.execute(f'DELETE FROM "{table}" WHERE mint=?', (mint,))
            db.execute("DELETE FROM solana_consensus_decisions WHERE mint=?", (mint,))
            db.execute("DELETE FROM solana_risk_evidence_reports WHERE mint=?", (mint,))
            db.execute("DELETE FROM solana_market_snapshots WHERE mint=?", (mint,))
            db.execute("DELETE FROM solana_launch_candidates WHERE mint=?", (mint,))
            db.execute("DELETE FROM launch_feed_events WHERE details LIKE ?", (f"%{mint}%",))
            db.execute("DELETE FROM paper_consensus_decisions WHERE decision_id=?", (decision_id,))
            db.execute("DELETE FROM events WHERE payload LIKE ?", (f"%{decision_id}%",))
