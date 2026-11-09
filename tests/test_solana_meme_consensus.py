import pytest

from veltrix.whales import WalletBuyEvent
from veltrix.solana_meme.consensus import (
    ConsensusEngine,
    MarketEvidence,
    RiskEvidence,
)
from veltrix.solana_meme.risk_evidence import RiskFlag

MINT = "So11111111111111111111111111111111111111112"
WALLET_A = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET_B = "Vote111111111111111111111111111111111111111"
NOW = 10_000_000


def market(**overrides):
    values = dict(
        mint=MINT,
        snapshot_id=12,
        pair_address="pool-1",
        price_usd=0.001,
        liquidity_usd=100_000,
        volume_h1_usd=50_000,
        price_change_m5_pct=5.0,
        retrieved_at_ms=NOW,
        pair_created_at_ms=NOW - 3_600_000,
        pair_count=1,
        quality_flags=(),
    )
    values.update(overrides)
    return MarketEvidence(**values)


def risk(**overrides):
    values = dict(
        mint=MINT,
        observed_at_ms=NOW,
        risk_state="EVIDENCE_COLLECTED_REVIEW_REQUIRED",
        missing_claims=(),
        errors=(),
        risk_flags=(),
        evidence_hash="risk-evidence-hash",
    )
    values.update(overrides)
    return RiskEvidence(**values)


def history():
    return [
        {"volume_h1_usd": 100.0, "retrieved_at_ms": NOW - (index + 1) * 60_000}
        for index in range(5)
    ]


def wallet_events():
    return [
        WalletBuyEvent("solana", MINT, WALLET_A, NOW - 100, 150, "helius"),
        WalletBuyEvent("solana", MINT, WALLET_B, NOW - 50, 200, "helius"),
    ]


def full_engine():
    return ConsensusEngine(
        trusted_wallets=(WALLET_A, WALLET_B),
        min_liquidity_usd=10_000,
        min_volume_h1_usd=10_000,
        min_pair_age_ms=60_000,
        whale_min_buy_usd=100,
    )


def test_four_agents_can_reach_analysis_consensus_without_authorizing_execution():
    decision = full_engine().evaluate(
        market(), risk(), history=history(),
        wallet_events=wallet_events(), now_ms=NOW,
    )

    assert decision.status == "CONSENSUS_APPROVED"
    assert decision.support_count == 3
    assert decision.supporting_agents == ("scout", "whale", "sniper")
    assert decision.paper_review_eligible is True
    assert decision.execution_authorized is False
    assert {vote.agent for vote in decision.votes} == {"scout", "whale", "sniper", "shield"}
    assert next(vote for vote in decision.votes if vote.agent == "shield").stance == "APPROVE"


def test_missing_security_claims_limit_positive_market_signals_to_watch():
    decision = full_engine().evaluate(
        market(),
        risk(missing_claims=("liquidity_lock_complete_inventory", "creator_wallet_provenance")),
        history=history(),
        wallet_events=(),
        now_ms=NOW,
    )
    assert decision.status == "WATCH"
    assert decision.support_count == 1
    assert decision.paper_review_eligible is False
    assert decision.execution_authorized is False
    assert next(vote for vote in decision.votes if vote.agent == "shield").stance == "ABSTAIN"


def test_critical_security_flag_vetoes_other_agent_support():
    flag = RiskFlag(
        "freeze_authority_active", "critical",
        "Freeze authority remains active", "solana-finalized-mint-account",
    )
    decision = full_engine().evaluate(
        market(), risk(risk_flags=(flag,)),
        history=history(), wallet_events=wallet_events(), now_ms=NOW,
    )
    assert decision.status == "REJECTED"
    assert decision.paper_review_eligible is False
    assert decision.execution_authorized is False
    assert next(vote for vote in decision.votes if vote.agent == "shield").stance == "REJECT"


def test_high_risk_flag_also_vetoes_consensus():
    flag = RiskFlag("token2022_transfer_hook", "high", "Transfer hook", "mint")
    decision = full_engine().evaluate(
        market(), risk(risk_flags=(flag,)),
        history=history(), wallet_events=wallet_events(), now_ms=NOW,
    )
    assert decision.status == "REJECTED"


def test_no_history_or_allowlisted_wallet_flow_does_not_create_votes():
    decision = ConsensusEngine().evaluate(
        market(), risk(missing_claims=("liquidity_lock_complete_inventory",)),
        history=(), wallet_events=(), now_ms=NOW,
    )
    assert decision.status == "ABSTAIN"
    assert decision.support_count == 0
    assert all(vote.stance == "ABSTAIN" for vote in decision.votes)


def test_stale_market_evidence_abstains():
    decision = full_engine().evaluate(
        market(retrieved_at_ms=NOW - 200_000),
        risk(),
        history=history(),
        wallet_events=(),
        now_ms=NOW,
    )
    assert decision.status == "ABSTAIN"
    assert next(vote for vote in decision.votes if vote.agent == "scout").stance == "ABSTAIN"


def test_stale_risk_evidence_cannot_approve():
    decision = full_engine().evaluate(
        market(),
        risk(observed_at_ms=NOW - 600_000),
        history=history(),
        wallet_events=wallet_events(),
        now_ms=NOW,
    )
    assert decision.status == "WATCH"
    assert decision.execution_authorized is False
    assert next(vote for vote in decision.votes if vote.agent == "shield").stance == "ABSTAIN"


def test_medium_risk_flag_requires_review_instead_of_approval():
    flag = RiskFlag("token2022_transfer_fee_config", "medium", "Transfer fee", "mint")
    decision = full_engine().evaluate(
        market(), risk(risk_flags=(flag,)),
        history=history(), wallet_events=wallet_events(), now_ms=NOW,
    )
    assert decision.status == "WATCH"
    assert next(vote for vote in decision.votes if vote.agent == "shield").stance == "ABSTAIN"


def test_market_and_risk_mint_must_match():
    with pytest.raises(ValueError, match="mint mismatch"):
        full_engine().evaluate(
            market(),
            risk(mint="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
            history=history(),
            now_ms=NOW,
        )


def test_market_evidence_from_store_row_validates_persisted_shape():
    row = {
        "id": 12,
        "mint": MINT,
        "pair_address": "pool-1",
        "price_usd": 0.001,
        "liquidity_usd": 100_000,
        "volume_h1_usd": 50_000,
        "price_change_m5_pct": None,
        "retrieved_at_ms": NOW,
        "pair_created_at_ms": NOW - 3_600_000,
        "pair_count": 1,
        "quality_flags": [],
    }
    evidence = MarketEvidence.from_store_row(row)
    assert evidence.price_change_m5_pct is None
    assert evidence.liquidity_usd == 100_000
    with pytest.raises(ValueError):
        MarketEvidence.from_store_row({**row, "price_usd": float("nan")})


def test_risk_evidence_from_store_payload_preserves_flags_and_claims():
    payload = {
        "mint": MINT,
        "observed_at_ms": NOW,
        "risk_state": "REQUIRES_MORE_EVIDENCE",
        "missing_claims": ["developer_wallet_linkage"],
        "errors": [],
        "risk_flags": [{
            "code": "mint_authority_active",
            "severity": "high",
            "description": "Mint authority remains active",
            "evidence_source": "solana-finalized-mint-account",
        }],
        "mint_evidence_id": "mint-evidence",
        "holder_evidence_id": None,
    }
    evidence = RiskEvidence.from_store_payload(payload)
    assert evidence.missing_claims == ("developer_wallet_linkage",)
    assert evidence.risk_flags[0].severity == "high"
    assert len(evidence.evidence_hash) == 64
    with pytest.raises(ValueError):
        RiskEvidence.from_store_payload({**payload, "risk_flags": [{"code": "bad"}]})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_liquidity_usd": -1},
        {"min_volume_h1_usd": float("nan")},
        {"volume_multiple": 1.0},
        {"max_market_age_ms": 0},
        {"min_history": 2},
    ],
)
def test_consensus_engine_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        ConsensusEngine(**kwargs)

def test_market_quality_flags_block_consensus_approval():
    decision = full_engine().evaluate(
        market(quality_flags=("market_cap_missing",)),
        risk(),
        history=history(),
        wallet_events=wallet_events(),
        now_ms=NOW,
    )
    assert decision.status == "WATCH"
    assert decision.paper_review_eligible is False
    assert decision.execution_authorized is False


def test_risk_state_must_be_review_ready_even_when_claim_list_is_empty():
    decision = full_engine().evaluate(
        market(),
        risk(risk_state="REQUIRES_MORE_EVIDENCE"),
        history=history(),
        wallet_events=wallet_events(),
        now_ms=NOW,
    )
    assert decision.status == "WATCH"
    assert next(vote for vote in decision.votes if vote.agent == "shield").stance == "ABSTAIN"


def test_consensus_rejects_non_solana_market_evidence():
    with pytest.raises(ValueError, match="canonical Solana"):
        full_engine().evaluate(
            market(chain="base"), risk(), history=history(), now_ms=NOW
        )


def test_risk_payload_rejects_unknown_state():
    payload = {
        "mint": MINT,
        "observed_at_ms": NOW,
        "risk_state": "SAFE",
        "missing_claims": [],
        "errors": [],
        "risk_flags": [],
    }
    with pytest.raises(ValueError):
        RiskEvidence.from_store_payload(payload)
