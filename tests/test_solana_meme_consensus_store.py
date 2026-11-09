import pytest

from veltrix.solana_meme.consensus import ConsensusEngine, MarketEvidence, RiskEvidence
from veltrix.solana_meme.consensus_store import ConsensusStore

MINT = "So11111111111111111111111111111111111111112"


def decision(now, mint=MINT):
    market = MarketEvidence(
        mint=mint, snapshot_id=1, pair_address="pool",
        price_usd=0.001, liquidity_usd=1000, volume_h1_usd=100,
        price_change_m5_pct=0.1, retrieved_at_ms=now,
        pair_created_at_ms=now - 1000, pair_count=1, quality_flags=(),
    )
    risk = RiskEvidence(
        mint=mint, observed_at_ms=now,
        risk_state="REQUIRES_MORE_EVIDENCE",
        missing_claims=("liquidity_lock_complete_inventory",),
        errors=(), risk_flags=(), evidence_hash=f"risk-{now}",
    )
    return ConsensusEngine().evaluate(
        market, risk, history=(), now_ms=now
    )


def test_consensus_store_is_append_only_and_deduplicates_decision_id(tmp_path):
    store = ConsensusStore(tmp_path / "consensus.sqlite3")
    first = decision(1000)
    assert store.record(first)
    assert not store.record(first)
    assert store.count(MINT) == 1

    second = decision(2000)
    assert store.record(second)
    assert store.count(MINT) == 2


def test_consensus_store_preserves_votes_and_execution_disabled(tmp_path):
    store = ConsensusStore(tmp_path / "consensus.sqlite3")
    report = decision(1000)
    store.record(report)
    latest = store.latest(MINT)

    assert latest["status"] == "ABSTAIN"
    assert latest["execution_authorized"] is False
    assert latest["paper_review_eligible"] is False
    assert len(latest["votes"]) == 4
    assert latest["risk_state"] == "REQUIRES_MORE_EVIDENCE"


def test_consensus_store_history_is_newest_first_and_validates_limit(tmp_path):
    store = ConsensusStore(tmp_path / "consensus.sqlite3")
    store.record(decision(1000))
    store.record(decision(2000))
    assert [item["created_at_ms"] for item in store.history(MINT)] == [2000, 1000]
    with pytest.raises(ValueError):
        store.history(MINT, limit=0)


def test_consensus_store_requires_persistent_database():
    with pytest.raises(ValueError):
        ConsensusStore(":memory:")

def test_consensus_store_refuses_execution_authorization_and_inconsistent_eligibility(tmp_path):
    from dataclasses import replace

    store = ConsensusStore(tmp_path / "consensus.sqlite3")
    report = decision(1000)
    with pytest.raises(ValueError, match="cannot authorize execution"):
        store.record(replace(report, execution_authorized=True))
    with pytest.raises(ValueError, match="eligibility"):
        store.record(replace(report, paper_review_eligible=True))

def test_consensus_store_latest_all_returns_one_newest_decision_per_mint(tmp_path):
    store = ConsensusStore(tmp_path / "latest-all.sqlite3")
    mint_b = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    store.record(decision(1000))
    store.record(decision(2000, mint_b))
    store.record(decision(3000))
    latest = store.latest_all()
    assert [(row["mint"], row["created_at_ms"]) for row in latest] == [
        (MINT, 3000),
        (mint_b, 2000),
    ]
    with pytest.raises(ValueError):
        store.latest_all(limit=0)
