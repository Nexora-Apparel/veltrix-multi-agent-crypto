from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from veltrix.ledger import PaperBroker
from veltrix.whales import WalletBuyEvent
from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.consensus import ConsensusEngine, MarketEvidence, RiskEvidence
from veltrix.solana_meme.consensus_store import ConsensusStore
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.paper_trading import PaperTradingPolicy, PaperTradingWorker
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore

MINT = "So11111111111111111111111111111111111111112"
WALLET_A = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET_B = "Vote111111111111111111111111111111111111111"
NOW = 10_000_000


def nonapproved_decision(decision, *, created_at_ms):
    votes = list(decision.votes)
    votes[3] = replace(
        votes[3],
        stance="ABSTAIN",
        reason="Required security evidence is incomplete",
        evidence_refs=("risk:creator_wallet_provenance",),
    )
    changed = replace(
        decision,
        decision_id="0" * 64,
        status="WATCH",
        created_at_ms=created_at_ms,
        votes=tuple(votes),
        risk_state="REQUIRES_MORE_EVIDENCE",
        missing_claims=("creator_wallet_provenance",),
        reasons=("Security evidence requires manual review",),
        paper_review_eligible=False,
        execution_authorized=False,
    )
    payload = {
        "mint": changed.mint,
        "status": changed.status,
        "votes": [
            {
                "agent": vote.agent,
                "stance": vote.stance,
                "reason": vote.reason,
                "evidence_refs": vote.evidence_refs,
            }
            for vote in changed.votes
        ],
        "market_snapshot_id": changed.market_snapshot_id,
        "risk_evidence_hash": changed.risk_evidence_hash,
        "rule_version": changed.rule_version,
        "market_quality_flags": changed.market_quality_flags,
    }
    decision_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return replace(changed, decision_id=decision_id)


def market_snapshot(observed, *, price=0.001, volume=50_000, liquidity=100_000, quality_flags=()):
    return SolanaMarketSnapshot(
        mint=MINT,
        selected_pair_address="pool-1",
        symbol="MEME",
        name="Meme Token",
        price_usd=price,
        selected_pool_liquidity_usd=liquidity,
        selected_pool_volume_m5_usd=1_000,
        selected_pool_volume_h1_usd=volume,
        selected_pool_volume_h24_usd=200_000,
        price_change_m5_pct=5.0,
        price_change_h1_pct=10.0,
        price_change_h24_pct=20.0,
        txns_m5_buys=100,
        txns_m5_sells=40,
        txns_h1_buys=500,
        txns_h1_sells=200,
        txns_h24_buys=2_000,
        txns_h24_sells=1_000,
        fdv_usd=1_000_000,
        market_cap_usd=900_000,
        dex_id="raydium",
        quote_token_symbol="SOL",
        pool_token_side="base",
        pair_count=1,
        retrieved_at_ms=observed,
        response_received_at_ms=observed,
        pair_created_at_ms=NOW - 3_600_000,
        quality_flags=quality_flags,
    )


def complete_risk_report(observed):
    return SolanaRiskEvidenceReport(
        mint=MINT,
        observed_at_ms=observed,
        goplus_provider=None,
        goplus_evidence_id=None,
        goplus_observed_at_ms=None,
        liquidity_locked=None,
        goplus_dev_hold_pct=None,
        goplus_bundled_supply_pct=None,
        goplus_creator_hold_pct=None,
        holder_provider="test-holder-provider",
        holder_evidence_id="test-holder-evidence",
        holder_block_ref="finalized-slot:123",
        holder_count=1,
        total_supply_raw=1_000_000,
        observed_holder_balance_raw=1_000_000,
        holder_coverage_complete=True,
        top_holder_pct=None,
        top5_holder_pct=None,
        top10_holder_pct=None,
        missing_claims=(),
        errors=(),
        mint_evidence_id="finalized-mint",
        mint_finalized_slot=123,
        token_program_id="TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        mint_authority=None,
        freeze_authority=None,
        token_2022_extension_types=(),
        token_2022_extension_review_complete=True,
        mint_supply_raw=1_000_000,
        mint_decimals=9,
        mint_supply_reconciled=True,
        holder_decimals=9,
        risk_flags=(),
    )


def build_pipeline(tmp_path, *, approved=True, broker_balance=500):
    shared = tmp_path / "shared.sqlite3"
    markets = MarketSnapshotStore(shared)
    risks = SolanaRiskEvidenceStore(shared)
    consensus = ConsensusStore(shared)
    for index in range(5):
        markets.record(market_snapshot(NOW - (5 - index) * 60_000, volume=100))
    markets.record(market_snapshot(NOW))
    risks.record(complete_risk_report(NOW))

    latest = markets.latest(MINT)
    market = MarketEvidence.from_store_row(latest)
    risk = RiskEvidence.from_store_payload(risks.latest(MINT))
    engine = ConsensusEngine(
        trusted_wallets=(WALLET_A, WALLET_B),
        min_liquidity_usd=10_000,
        min_volume_h1_usd=10_000,
        min_pair_age_ms=60_000,
        whale_min_buy_usd=100,
    )
    decision = engine.evaluate(
        market,
        risk,
        history=[
            row for row in markets.history(MINT, limit=20)
            if row["id"] != latest["id"] and row["retrieved_at_ms"] < latest["retrieved_at_ms"]
        ],
        wallet_events=(
            WalletBuyEvent("solana", MINT, WALLET_A, NOW - 100, 150, "helius"),
            WalletBuyEvent("solana", MINT, WALLET_B, NOW - 50, 175, "helius"),
        ),
        now_ms=NOW,
    )
    if not approved:
        decision = nonapproved_decision(decision, created_at_ms=NOW)
    consensus.record(decision)
    broker = PaperBroker(
        str(tmp_path / "shared.sqlite3"),
        starting_balance=broker_balance,
        max_positions=10,
        max_position_usd=50,
    )
    worker = PaperTradingWorker(
        consensus,
        markets,
        risks,
        broker,
        policy=PaperTradingPolicy(
            position_usd=25,
            fee_bps=100,
            slippage_bps=50,
            max_decision_age_ms=120_000,
            max_market_age_ms=120_000,
            max_risk_age_ms=300_000,
        ),
        clock_ms=lambda: NOW,
    )
    return markets, risks, consensus, broker, worker, decision


def test_only_fresh_approved_consensus_opens_idempotent_paper_position(tmp_path):
    _, _, _, broker, worker, decision = build_pipeline(tmp_path)

    first = worker.run_once()
    second = worker.run_once()

    assert decision.status == "CONSENSUS_APPROVED"
    assert first.opened_positions == 1
    assert first.blocked_entries == 0
    assert second.opened_positions == 0
    assert second.duplicate_decisions == 1
    position = broker.snapshot()["open_positions"][0]
    assert position["entry_price"] == pytest.approx(0.001005)
    assert position["cost_basis"] == pytest.approx(25.25)
    assert broker.snapshot()["cash"] == pytest.approx(474.75)
    costs = broker.execution_cost_totals()
    assert costs["fees_usd"] == pytest.approx(0.25)
    assert costs["slippage_cost_usd"] > 0
    assert broker.paper_decisions(limit=1)[0]["execution_status"] == "OPENED"


def test_non_approved_consensus_is_recorded_without_opening(tmp_path):
    _, _, _, broker, worker, decision = build_pipeline(tmp_path, approved=False)

    result = worker.run_once()

    assert decision.status == "WATCH"
    assert result.opened_positions == 0
    assert result.skipped_decisions == 1
    assert broker.snapshot()["open_positions"] == []
    assert broker.paper_decisions(limit=1)[0]["execution_status"] == "NOT_ELIGIBLE"


def test_broker_blocks_entry_when_cash_cannot_cover_notional_and_fee(tmp_path):
    _, _, _, broker, worker, _ = build_pipeline(tmp_path, broker_balance=25)

    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.blocked_entries == 1
    assert broker.snapshot()["open_positions"] == []
    assert broker.paper_decisions(limit=1)[0]["execution_status"] == "BLOCKED"


def test_market_snapshot_change_after_consensus_blocks_entry(tmp_path):
    markets, _, _, broker, worker, _ = build_pipeline(tmp_path)
    markets.record(market_snapshot(NOW + 1, price=0.0011))

    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.blocked_entries == 1
    assert broker.snapshot()["open_positions"] == []


def test_exit_pipeline_applies_simulated_fee_and_slippage(tmp_path):
    markets, _, _, broker, worker, _ = build_pipeline(tmp_path)
    assert worker.run_once().opened_positions == 1

    markets.record(market_snapshot(NOW + 1, price=0.00201))
    worker.clock_ms = lambda: NOW + 1
    result = worker.run_once()

    assert result.exit_fills == 1
    position = broker.snapshot()["open_positions"][0]
    assert position["quantity"] < 25 / 0.001005
    costs = broker.execution_cost_totals()
    assert costs["fees_usd"] > 0.25
    assert costs["slippage_cost_usd"] > 0
    assert broker.totals()["closed_sell_fills"] == 1


def test_paper_policy_rejects_nonfinite_or_unbounded_costs():
    with pytest.raises(ValueError):
        PaperTradingPolicy(position_usd=float("nan"))
    with pytest.raises(ValueError):
        PaperTradingPolicy(fee_bps=2_001)
    with pytest.raises(ValueError):
        PaperTradingPolicy(slippage_bps=-1)


def test_paper_broker_decision_idempotency_and_cost_validation(tmp_path):
    markets, _, _, broker, _, decision = build_pipeline(tmp_path, broker_balance=100)
    market = markets.latest(MINT)
    kwargs = {
        "created_at_ms": decision.created_at_ms,
        "paper_review_eligible": True,
        "execution_authorized": False,
        "fee_bps": 100,
        "slippage_bps": 50,
        "processed_at_ms": NOW,
        "expected_market_snapshot_id": market["id"],
        "expected_risk_evidence_hash": decision.risk_evidence_hash,
    }
    opened = broker.open_position_from_consensus(
        decision.decision_id, MINT, market["price_usd"], 25, "approved", **kwargs
    )
    duplicate = broker.open_position_from_consensus(
        decision.decision_id, MINT, market["price_usd"], 25, "approved", **kwargs
    )
    assert opened["status"] == "OPENED"
    assert duplicate["status"] == "DUPLICATE"
    assert broker.snapshot()["cash"] == pytest.approx(74.75)
    with pytest.raises(ValueError):
        broker.open_position_from_consensus(
            "c" * 64, MINT, 1.0, 25, "approved",
            created_at_ms=NOW, paper_review_eligible=True,
            execution_authorized=False, fee_bps=float("nan"),
        )


def test_paper_broker_rejects_entry_without_atomic_evidence_references(tmp_path):
    broker = PaperBroker(str(tmp_path / "missing-reconciliation.sqlite3"), 100, 2, 50)
    with pytest.raises(ValueError, match="expected market snapshot ID and risk evidence hash"):
        broker.open_position_from_consensus(
            "a" * 64, MINT, 1.0, 25, "approved",
            created_at_ms=NOW, paper_review_eligible=True,
            execution_authorized=False,
        )


def test_newer_nonapproved_decision_prevents_execution_of_older_approval(tmp_path):
    _, _, consensus, broker, worker, approved = build_pipeline(tmp_path)
    newer = nonapproved_decision(approved, created_at_ms=NOW + 1)
    assert consensus.record(newer)

    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.skipped_decisions == 1
    assert broker.snapshot()["open_positions"] == []
    assert broker.paper_decisions(limit=1)[0]["decision_id"] == newer.decision_id


def test_security_evidence_change_after_approval_blocks_paper_entry(tmp_path):
    _, risks, _, broker, worker, _ = build_pipeline(tmp_path)
    assert risks.record(complete_risk_report(NOW + 1))
    worker.clock_ms = lambda: NOW + 1

    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.blocked_entries == 1
    assert broker.snapshot()["open_positions"] == []
    assert broker.paper_decisions(limit=1)[0]["execution_status"] == "BLOCKED"

def test_paper_worker_rejects_split_phase_databases(tmp_path):
    shared = tmp_path / "phase-data.sqlite3"
    markets = MarketSnapshotStore(shared)
    risks = SolanaRiskEvidenceStore(shared)
    consensus = ConsensusStore(shared)
    broker = PaperBroker(str(tmp_path / "separate-paper.sqlite3"))

    with pytest.raises(ValueError, match="must share one persistent SQLite"):
        PaperTradingWorker(consensus, markets, risks, broker)

def test_broker_rechecks_latest_market_snapshot_inside_entry_transaction(tmp_path):
    markets, _, _, broker, worker, _ = build_pipeline(tmp_path)
    original = broker.open_position_from_consensus

    def race_market_update(*args, **kwargs):
        markets.record(market_snapshot(NOW + 1, price=0.0012))
        return original(*args, **kwargs)

    broker.open_position_from_consensus = race_market_update
    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.blocked_entries == 1
    assert broker.snapshot()["open_positions"] == []
    assert "market snapshot changed" in broker.paper_decisions(limit=1)[0]["reason"]


def test_broker_rechecks_latest_consensus_inside_entry_transaction(tmp_path):
    markets, _, consensus, broker, worker, decision = build_pipeline(tmp_path)
    original = broker.open_position_from_consensus

    def race_newer_abstention(*args, **kwargs):
        newer = nonapproved_decision(decision, created_at_ms=NOW + 1)
        consensus.record(newer)
        return original(*args, **kwargs)

    broker.open_position_from_consensus = race_newer_abstention
    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.blocked_entries == 1
    assert broker.snapshot()["open_positions"] == []
    assert "newer consensus decision" in broker.paper_decisions(limit=1)[0]["reason"]


def test_status_marks_open_positions_to_latest_fresh_market_price(tmp_path):
    markets, _, _, broker, worker, _ = build_pipeline(tmp_path)
    assert worker.run_once().opened_positions == 1
    markets.record(market_snapshot(NOW + 1, price=0.0015))
    worker.clock_ms = lambda: NOW + 1

    status = worker.status()

    assert status["unmarked_positions"] == []
    position = status["account"]["open_positions"][0]
    assert position["mark_price"] == pytest.approx(0.0015)
    assert status["account"]["unrealized_pnl"] > 0
    assert status["account"]["equity"] > status["account"]["cash"]

def test_paper_broker_rejects_missing_or_enabled_execution_authority(tmp_path):
    broker = PaperBroker(str(tmp_path / "approval-boundary.sqlite3"), 100, 2, 50)
    with pytest.raises(ValueError, match="paper_review_eligible"):
        broker.open_position_from_consensus(
            "a" * 64, MINT, 1.0, 25, "approved",
            created_at_ms=NOW, execution_authorized=False,
        )
    with pytest.raises(ValueError, match="execution_authorized"):
        broker.open_position_from_consensus(
            "b" * 64, MINT, 1.0, 25, "approved",
            created_at_ms=NOW, paper_review_eligible=True, execution_authorized=True,
        )

def test_broker_enforces_total_portfolio_exposure_cap_and_reports_exposure(tmp_path):
    markets, _, _, broker, _, decision = build_pipeline(tmp_path, broker_balance=100)
    # Seed an existing virtual position so this test exercises the portfolio
    # cap while the new entry still passes the complete consensus gate.
    with sqlite3.connect(broker.db_path) as db:
        db.execute("UPDATE account SET cash = 60 WHERE id = 1")
        db.execute(
            "INSERT INTO positions(token,entry_price,quantity,cost_basis,opened_at) VALUES(?,?,?,?,?)",
            ("existing-token", 1.0, 40.0, 40.0, NOW / 1000),
        )
        db.execute(
            "INSERT INTO position_state(token,peak_price) VALUES(?,?)",
            ("existing-token", 1.0),
        )
    market = markets.latest(MINT)
    result = broker.open_position_from_consensus(
        decision.decision_id, MINT, market["price_usd"], 20, "approved entry",
        created_at_ms=decision.created_at_ms,
        paper_review_eligible=True, execution_authorized=False,
        fee_bps=0, slippage_bps=0, processed_at_ms=NOW,
        expected_market_snapshot_id=market["id"],
        expected_risk_evidence_hash=decision.risk_evidence_hash,
        max_portfolio_exposure_pct=0.5,
    )

    assert result["status"] == "BLOCKED"
    assert "exposure cap" in result["reason"]
    snapshot = broker.snapshot()
    assert snapshot["portfolio_exposure_usd"] == pytest.approx(40)
    assert snapshot["portfolio_exposure_pct"] == pytest.approx(40)


def test_worker_blocks_entry_that_would_exceed_portfolio_exposure_policy(tmp_path):
    _, _, _, broker, worker, _ = build_pipeline(tmp_path)
    worker.policy = PaperTradingPolicy(
        position_usd=25,
        fee_bps=100,
        slippage_bps=50,
        max_portfolio_exposure_pct=0.05,
    )

    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.blocked_entries == 1
    assert broker.snapshot()["open_positions"] == []
    assert "exposure cap" in broker.paper_decisions(limit=1)[0]["reason"]

def test_broker_atomically_rechecks_latest_market_snapshot(tmp_path):
    markets, _, _, broker, _, decision = build_pipeline(tmp_path)
    market = markets.latest(MINT)
    markets.record(market_snapshot(NOW + 1, price=0.0012))
    result = broker.open_position_from_consensus(
        decision.decision_id, MINT, market["price_usd"], 25, "approved",
        created_at_ms=decision.created_at_ms,
        paper_review_eligible=True, execution_authorized=False,
        expected_market_snapshot_id=market["id"],
        expected_risk_evidence_hash=decision.risk_evidence_hash,
        processed_at_ms=NOW + 1,
    )
    assert result["status"] == "BLOCKED"
    assert "market snapshot changed" in result["reason"]
    assert broker.snapshot()["open_positions"] == []


def test_broker_atomically_rechecks_latest_risk_evidence_hash(tmp_path):
    markets, risks, _, broker, _, decision = build_pipeline(tmp_path)
    market = markets.latest(MINT)
    assert risks.record(complete_risk_report(NOW + 1))
    result = broker.open_position_from_consensus(
        decision.decision_id, MINT, market["price_usd"], 25, "approved",
        created_at_ms=decision.created_at_ms,
        paper_review_eligible=True, execution_authorized=False,
        expected_market_snapshot_id=market["id"],
        expected_risk_evidence_hash=decision.risk_evidence_hash,
        processed_at_ms=NOW + 1,
    )
    assert result["status"] == "BLOCKED"
    assert "security evidence changed" in result["reason"]
    assert broker.snapshot()["open_positions"] == []


def test_broker_atomically_rechecks_latest_consensus_decision(tmp_path):
    markets, _, consensus, broker, _, decision = build_pipeline(tmp_path)
    market = markets.latest(MINT)
    newer = nonapproved_decision(decision, created_at_ms=NOW + 1)
    assert consensus.record(newer)
    result = broker.open_position_from_consensus(
        decision.decision_id, MINT, market["price_usd"], 25, "approved",
        created_at_ms=decision.created_at_ms,
        paper_review_eligible=True, execution_authorized=False,
        expected_market_snapshot_id=market["id"],
        expected_risk_evidence_hash=decision.risk_evidence_hash,
        processed_at_ms=NOW + 1,
    )
    assert result["status"] == "BLOCKED"
    assert "newer consensus decision superseded" in result["reason"]
    assert broker.snapshot()["open_positions"] == []



def test_broker_revalidates_consensus_payload_inside_entry_transaction(tmp_path):
    _, _, _, broker, worker, decision = build_pipeline(tmp_path)
    original = broker.open_position_from_consensus

    def tamper_consensus(*args, **kwargs):
        with sqlite3.connect(broker.db_path) as db:
            row = db.execute(
                "SELECT id, payload_json FROM solana_consensus_decisions WHERE decision_id = ?",
                (decision.decision_id,),
            ).fetchone()
            payload = json.loads(row[1])
            payload["status"] = "WATCH"
            payload["paper_review_eligible"] = False
            db.execute(
                "UPDATE solana_consensus_decisions SET payload_json = ? WHERE id = ?",
                (json.dumps(payload), row[0]),
            )
        return original(*args, **kwargs)

    broker.open_position_from_consensus = tamper_consensus
    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.blocked_entries == 1
    assert broker.snapshot()["open_positions"] == []
    assert "consensus decision failed atomic validation" in broker.paper_decisions(limit=1)[0]["reason"]


def test_broker_revalidates_risk_payload_inside_entry_transaction(tmp_path):
    _, _, _, broker, worker, decision = build_pipeline(tmp_path)
    original = broker.open_position_from_consensus

    def tamper_risk(*args, **kwargs):
        with sqlite3.connect(broker.db_path) as db:
            row = db.execute(
                "SELECT id, payload_json FROM solana_risk_evidence_reports WHERE mint = ?",
                (MINT,),
            ).fetchone()
            payload = json.loads(row[1])
            payload["risk_state"] = "REQUIRES_MORE_EVIDENCE"
            payload["missing_claims"] = ["creator_wallet_provenance"]
            db.execute(
                "UPDATE solana_risk_evidence_reports SET payload_json = ? WHERE id = ?",
                (json.dumps(payload), row[0]),
            )
        return original(*args, **kwargs)

    broker.open_position_from_consensus = tamper_risk
    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.blocked_entries == 1
    assert broker.snapshot()["open_positions"] == []
    assert "security evidence" in broker.paper_decisions(limit=1)[0]["reason"]


def test_broker_revalidates_market_quality_inside_entry_transaction(tmp_path):
    _, _, _, broker, worker, _ = build_pipeline(tmp_path)
    original = broker.open_position_from_consensus

    def tamper_market(*args, **kwargs):
        with sqlite3.connect(broker.db_path) as db:
            row = db.execute(
                "SELECT id FROM solana_market_snapshots WHERE mint = ? ORDER BY retrieved_at_ms DESC, id DESC LIMIT 1",
                (MINT,),
            ).fetchone()
            db.execute(
                "UPDATE solana_market_snapshots SET quality_flags_json = ? WHERE id = ?",
                (json.dumps(["liquidity_usd_missing"]), row[0]),
            )
        return original(*args, **kwargs)

    broker.open_position_from_consensus = tamper_market
    result = worker.run_once()

    assert result.opened_positions == 0
    assert result.blocked_entries == 1
    assert broker.snapshot()["open_positions"] == []
    assert "market snapshot changed or has quality flags" in broker.paper_decisions(limit=1)[0]["reason"]


def test_paper_exits_and_status_ignore_market_snapshots_with_quality_flags(tmp_path):
    markets, _, _, broker, worker, _ = build_pipeline(tmp_path)
    assert worker.run_once().opened_positions == 1
    markets.record(
        market_snapshot(
            NOW + 1,
            price=0.0008,
            quality_flags=("liquidity_usd_missing",),
        )
    )
    worker.clock_ms = lambda: NOW + 1

    result = worker.run_once()
    status = worker.status()

    assert result.exit_fills == 0
    assert broker.snapshot()["open_positions"]
    assert status["unmarked_positions"] == [MINT]
