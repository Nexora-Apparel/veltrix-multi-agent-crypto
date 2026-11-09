from dataclasses import replace

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


def market_snapshot(observed, *, price=0.001, volume=50_000, liquidity=100_000):
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
        quality_flags=(),
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
        holder_provider=None,
        holder_evidence_id=None,
        holder_block_ref=None,
        holder_count=None,
        total_supply_raw=None,
        observed_holder_balance_raw=None,
        holder_coverage_complete=False,
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
        decision = replace(
            decision,
            decision_id=decision.decision_id + "-not-approved",
            status="ABSTAIN",
            paper_review_eligible=False,
        )
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

    assert decision.status == "ABSTAIN"
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
    broker = PaperBroker(str(tmp_path / "broker.sqlite3"), 100, 2, 50)
    opened = broker.open_position_from_consensus(
        "decision-1", MINT, 1.0, 25, "approved",
        created_at_ms=NOW, fee_bps=100, slippage_bps=50,
        processed_at_ms=NOW,
    )
    duplicate = broker.open_position_from_consensus(
        "decision-1", MINT, 1.0, 25, "approved",
        created_at_ms=NOW, fee_bps=100, slippage_bps=50,
        processed_at_ms=NOW,
    )
    assert opened["status"] == "OPENED"
    assert duplicate["status"] == "DUPLICATE"
    assert broker.snapshot()["cash"] == pytest.approx(74.75)
    with pytest.raises(ValueError):
        broker.open_position_from_consensus(
            "decision-2", MINT, 1.0, 25, "approved",
            created_at_ms=NOW, fee_bps=float("nan"),
        )

def test_newer_nonapproved_decision_prevents_execution_of_older_approval(tmp_path):
    _, _, consensus, broker, worker, approved = build_pipeline(tmp_path)
    newer = replace(
        approved,
        decision_id="f" * 64,
        created_at_ms=NOW + 1,
        status="ABSTAIN",
        paper_review_eligible=False,
    )
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
