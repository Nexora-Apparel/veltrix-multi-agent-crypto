from dataclasses import fields
import json
import sqlite3
import hashlib

import pytest

from veltrix.solana_meme.consensus import ConsensusDecision, ConsensusEngine, MarketEvidence, RiskEvidence
from veltrix.solana_meme.consensus_store import ConsensusStore

MINT = "So11111111111111111111111111111111111111112"


def decision(now, mint=MINT):
    market = MarketEvidence(
        mint=mint, snapshot_id=1, pair_address="pool",
        price_usd=0.001, liquidity_usd=100_000, volume_h1_usd=50_000,
        price_change_m5_pct=0.1, retrieved_at_ms=now,
        pair_created_at_ms=max(1, now - 1000), pair_count=1, quality_flags=(),
    )
    risk = RiskEvidence(
        mint=mint, observed_at_ms=now,
        risk_state="REQUIRES_MORE_EVIDENCE",
        missing_claims=("liquidity_lock_complete_inventory",),
        errors=(), risk_flags=(), evidence_hash=hashlib.sha256(f"risk-{now}".encode()).hexdigest(),
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



def test_consensus_store_revalidates_forged_decision_id_before_write(tmp_path):
    store = ConsensusStore(tmp_path / "consensus.sqlite3")
    original = decision(1000)
    forged = object.__new__(ConsensusDecision)
    for field in fields(ConsensusDecision):
        object.__setattr__(forged, field.name, getattr(original, field.name))
    object.__setattr__(forged, "decision_id", "0" * 64)

    with pytest.raises(ValueError, match="decision ID"):
        store.record(forged)
    assert store.count(MINT) == 0


def test_consensus_store_rejects_forged_shield_approval_with_medium_risk(tmp_path):
    from dataclasses import replace

    from veltrix.solana_meme.risk_evidence import RiskFlag

    store = ConsensusStore(tmp_path / "consensus.sqlite3")
    from veltrix.whales import WalletBuyEvent

    wallet_a = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    wallet_b = "Vote111111111111111111111111111111111111111"
    approved = ConsensusEngine(
        trusted_wallets=(wallet_a, wallet_b),
        min_liquidity_usd=10_000,
        min_volume_h1_usd=10_000,
        min_pair_age_ms=60_000,
        whale_min_buy_usd=100,
    ).evaluate(
        MarketEvidence(
            mint=MINT, snapshot_id=1, pair_address="pool",
            price_usd=0.001, liquidity_usd=100_000, volume_h1_usd=50_000,
            price_change_m5_pct=5.0, retrieved_at_ms=10_000_000,
            pair_created_at_ms=6_400_000, pair_count=1, quality_flags=(),
        ),
        RiskEvidence(
            mint=MINT, observed_at_ms=10_000_000,
            risk_state="EVIDENCE_COLLECTED_REVIEW_REQUIRED",
            missing_claims=(), errors=(), risk_flags=(),
            evidence_hash=hashlib.sha256(b"risk-approval-test").hexdigest(),
        ),
        history=[
            {"volume_h1_usd": 100.0, "retrieved_at_ms": 10_000_000 - (index + 1) * 60_000}
            for index in range(5)
        ],
        wallet_events=(
            WalletBuyEvent("solana", MINT, wallet_a, 9_999_900, 150, "helius"),
            WalletBuyEvent("solana", MINT, wallet_b, 9_999_950, 200, "helius"),
        ),
        now_ms=10_000_000,
    )
    assert approved.status == "CONSENSUS_APPROVED"
    # Risk flags are not part of the decision ID; the store must still enforce
    # the Shield gate against the persisted evidence envelope.
    forged = replace(
        approved,
        risk_flags=(RiskFlag("medium_flag", "medium", "Needs review", "test"),),
    )
    with pytest.raises(ValueError, match="Shield approval"):
        store.record(forged)
    assert store.count(MINT) == 0



def test_consensus_store_revalidates_payloads_on_read(tmp_path):
    store = ConsensusStore(tmp_path / "consensus.sqlite3")
    store.record(decision(1000))
    with sqlite3.connect(store.database_path) as db:
        row = db.execute(
            "SELECT id, payload_json FROM solana_consensus_decisions WHERE mint = ?",
            (MINT,),
        ).fetchone()
        payload = json.loads(row[1])
        payload["support_count"] = 3.0
        db.execute(
            "UPDATE solana_consensus_decisions SET payload_json = ? WHERE id = ?",
            (json.dumps(payload), row[0]),
        )

    with pytest.raises(ValueError, match="persisted consensus payload"):
        store.latest(MINT)
    with pytest.raises(ValueError, match="persisted consensus payload"):
        store.history(MINT)
    with pytest.raises(ValueError, match="persisted consensus payload"):
        store.latest_all()
