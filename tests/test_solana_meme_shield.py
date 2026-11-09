from dataclasses import replace

from veltrix.agents import Signal
from veltrix.safety import SafetyReport
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport
from veltrix.solana_meme.shield import SolanaShieldPolicy

MINT = "So11111111111111111111111111111111111111112"


def evidence():
    return SolanaRiskEvidenceReport(
        mint=MINT, observed_at_ms=10_000,
        goplus_provider="goplus", goplus_evidence_id="gp:1",
        goplus_observed_at_ms=10_000, liquidity_locked=True,
        holder_provider="rpc", holder_evidence_id="holders:1",
        holder_block_ref="finalized:1", holder_count=2,
        total_supply_raw=100, observed_holder_balance_raw=100,
        holder_coverage_complete=True, top_holder_pct=60.0,
        top5_holder_pct=100.0, top10_holder_pct=100.0,
        missing_claims=(), errors=(),
    )


def safety():
    return SafetyReport(
        token_address=MINT, passed=True, checks=("all",), reasons=(),
        observed_at_ms=10_000, composite_verified=True,
    )


def signals():
    return [
        Signal("scout", MINT, "BUY", "momentum"),
        Signal("whale", MINT, "BUY", "wallet activity"),
    ]


def test_two_agent_consensus_can_only_approve_paper_entry():
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=0, amount=50, now_ms=10_000,
    )
    assert result.decision == "APPROVE_PAPER_ENTRY"
    assert result.approved_for_paper is True
    assert result.approving_agents == ("scout", "whale")


def test_unknown_risk_claims_veto_even_with_positive_safety_report():
    incomplete = replace(evidence(), missing_claims=("mint_authority",))
    result = SolanaShieldPolicy().evaluate(
        MINT, incomplete, safety(), signals(),
        open_positions=0, amount=10, now_ms=10_000,
    )
    assert result.decision == "BLOCK"
    assert "mandatory risk claims remain unknown" in result.reasons


def test_missing_or_unverified_composite_safety_vetoes():
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), None, signals(),
        open_positions=0, amount=10, now_ms=10_000,
    )
    assert result.decision == "BLOCK"


def test_one_agent_cannot_approve():
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(), signals()[:1],
        open_positions=0, amount=10, now_ms=10_000,
    )
    assert result.decision == "BLOCK"
    assert "insufficient distinct BUY-agent consensus" in result.reasons


def test_position_limits_and_stale_evidence_veto():
    policy = SolanaShieldPolicy()
    full = policy.evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=10, amount=10, now_ms=10_000,
    )
    assert full.decision == "BLOCK"
    oversized = policy.evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=0, amount=51, now_ms=10_000,
    )
    assert oversized.decision == "BLOCK"
    stale = policy.evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=0, amount=10, now_ms=400_001,
    )
    assert stale.decision == "BLOCK"


def test_cross_mint_signal_vetoes():
    other = "11111111111111111111111111111111"
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(),
        [Signal("scout", MINT, "BUY", "a"), Signal("whale", other, "BUY", "b")],
        open_positions=0, amount=10, now_ms=10_000,
    )
    assert result.decision == "BLOCK"
    assert "BUY signals do not unanimously target this mint" in result.reasons


def test_invalid_amount_is_rejected():
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=0, amount=float("nan"), now_ms=10_000,
    )
    assert result.decision == "BLOCK"

def test_liquidity_lock_must_be_positively_verified():
    for value in (False, None):
        result = SolanaShieldPolicy().evaluate(
            MINT, replace(evidence(), liquidity_locked=value), safety(), signals(),
            open_positions=0, amount=10, now_ms=10_000,
        )
        assert result.decision == "BLOCK"


def test_missing_evidence_provenance_vetoes_even_complete_claims():
    incomplete = replace(evidence(), holder_evidence_id="")
    result = SolanaShieldPolicy().evaluate(
        MINT, incomplete, safety(), signals(),
        open_positions=0, amount=10, now_ms=10_000,
    )
    assert result.decision == "BLOCK"
    assert "risk evidence provenance is incomplete" in result.reasons
