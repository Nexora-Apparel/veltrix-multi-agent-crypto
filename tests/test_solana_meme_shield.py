from dataclasses import replace

from veltrix.agents import Signal
from veltrix.safety import SafetyReport
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport
from veltrix.solana_meme.shield import SolanaShieldPolicy
from veltrix.solana_meme.sellability import SolanaSellabilityEvidence
from veltrix.providers.solana_mint import TOKEN_PROGRAM_ID

MINT = "So11111111111111111111111111111111111111112"


def evidence():
    return SolanaRiskEvidenceReport(
        mint=MINT, observed_at_ms=10_000,
        goplus_provider="goplus", goplus_evidence_id="gp:1",
        goplus_observed_at_ms=10_000, liquidity_locked=True,
        holder_provider="rpc", holder_evidence_id="holders:1",
        holder_block_ref="finalized:1", holder_count=2,
        total_supply_raw=1_000_000_000, observed_holder_balance_raw=1_000_000_000,
        holder_coverage_complete=True, top_holder_pct=60.0,
        top5_holder_pct=100.0, top10_holder_pct=100.0,
        missing_claims=(), errors=(),
        mint_evidence_id="mint:1", mint_finalized_slot=10,
        token_program_id=TOKEN_PROGRAM_ID, mint_authority=None, freeze_authority=None,
        token_2022_extension_types=(), token_2022_extension_review_complete=True,
    )


def safety():
    return SafetyReport(
        token_address=MINT, passed=True, checks=("all",), reasons=(),
        observed_at_ms=10_000, composite_verified=True,
    )


def route(**changes):
    values = dict(
        mint=MINT, observed_at_ms=10_000, provider="jupiter-v2",
        evidence_id="route:abc", input_amount_raw=10_000_000,
        input_supply_fraction=0.01, quoted_output_raw=50_000_000,
        minimum_output_raw=49_000_000, route_plan_count=2,
        price_impact_bps=75.0, quote_expires_at_ms=20_000,
        state="ROUTE_OBSERVED_NOT_EXECUTION_PROVEN", errors=(), execution_proven=False,
    )
    values.update(changes)
    return SolanaSellabilityEvidence(**values)


def signals():
    return [
        Signal("scout", MINT, "BUY", "momentum"),
        Signal("whale", MINT, "BUY", "wallet activity"),
    ]


def test_two_agent_consensus_can_only_approve_paper_entry():
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=0, amount=50, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert result.decision == "APPROVE_PAPER_ENTRY"
    assert result.approved_for_paper is True
    assert result.approving_agents == ("scout", "whale")


def test_unknown_risk_claims_veto_even_with_positive_safety_report():
    incomplete = replace(evidence(), missing_claims=("mint_authority",))
    result = SolanaShieldPolicy().evaluate(
        MINT, incomplete, safety(), signals(),
        open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert result.decision == "BLOCK"
    assert "mandatory token-risk claims remain unknown" in result.reasons


def test_missing_or_unverified_composite_safety_vetoes():
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), None, signals(),
        open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert result.decision == "BLOCK"


def test_one_agent_cannot_approve():
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(), signals()[:1],
        open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert result.decision == "BLOCK"
    assert "insufficient distinct BUY-agent consensus" in result.reasons


def test_position_limits_and_stale_evidence_veto():
    policy = SolanaShieldPolicy()
    full = policy.evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=10, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert full.decision == "BLOCK"
    oversized = policy.evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=0, amount=51, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert oversized.decision == "BLOCK"
    stale = policy.evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=0, amount=10, now_ms=400_001, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert stale.decision == "BLOCK"


def test_cross_mint_signal_vetoes():
    other = "11111111111111111111111111111111"
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(),
        [Signal("scout", MINT, "BUY", "a"), Signal("whale", other, "BUY", "b")],
        open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert result.decision == "BLOCK"
    assert "BUY signals do not unanimously target this mint" in result.reasons


def test_invalid_amount_is_rejected():
    result = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=0, amount=float("nan"), now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert result.decision == "BLOCK"

def test_liquidity_lock_must_be_positively_verified():
    for value in (False, None):
        result = SolanaShieldPolicy().evaluate(
            MINT, replace(evidence(), liquidity_locked=value), safety(), signals(),
            open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
        )
        assert result.decision == "BLOCK"


def test_missing_evidence_provenance_vetoes_even_complete_claims():
    incomplete = replace(evidence(), holder_evidence_id="")
    result = SolanaShieldPolicy().evaluate(
        MINT, incomplete, safety(), signals(),
        open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert result.decision == "BLOCK"
    assert "risk evidence provenance is incomplete" in result.reasons

def test_invalid_evaluation_timestamp_and_boolean_supply_are_rejected():
    policy = SolanaShieldPolicy()
    invalid_time = policy.evaluate(
        MINT, evidence(), safety(), signals(),
        open_positions=0, amount=10, now_ms="not-a-timestamp", sellability=route(), sell_amount_raw=10_000_000,
    )
    assert invalid_time.decision == "BLOCK"
    boolean_supply = replace(
        evidence(), total_supply_raw=True, observed_holder_balance_raw=True,
    )
    invalid_supply = policy.evaluate(
        MINT, boolean_supply, safety(), signals(),
        open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert invalid_supply.decision == "BLOCK"



def test_active_mint_or_freeze_authority_vetoes_entry():
    for changes, expected in (
        ({"mint_authority": "11111111111111111111111111111111"}, "mint authority remains active"),
        ({"freeze_authority": "11111111111111111111111111111111"}, "freeze authority remains active"),
    ):
        result = SolanaShieldPolicy().evaluate(
            MINT, replace(evidence(), **changes), safety(), signals(),
            open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
        )
        assert result.decision == "BLOCK"
        assert expected in result.reasons


def test_token_2022_extension_presence_requires_separate_policy_review():
    result = SolanaShieldPolicy().evaluate(
        MINT,
        replace(
            evidence(),
            token_program_id="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
            token_2022_extension_types=("permanent_delegate",),
            token_2022_extension_review_complete=False,
        ),
        safety(), signals(), open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
    )
    assert result.decision == "BLOCK"
    assert "Token-2022 extensions require separate policy certification" in result.reasons


def test_route_observation_is_required_but_never_marks_execution_proven():
    blocked = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(), signals(), open_positions=0, amount=10, now_ms=10_000,
    )
    assert blocked.decision == "BLOCK"
    assert "typed sell-route evidence is required for paper entry" in blocked.reasons
    approved = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(), signals(), open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
        sellability=route(), sell_amount_raw=10_000_000,
    )
    assert approved.decision == "APPROVE_PAPER_ENTRY"
    assert route().state == "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN"
    assert route().execution_proven is False


def test_route_quote_must_match_exit_size_and_unknown_token_risk_still_vetoes():
    mismatch = SolanaShieldPolicy().evaluate(
        MINT, evidence(), safety(), signals(), open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
        sellability=route(input_amount_raw=9_000_000), sell_amount_raw=10_000_000,
    )
    assert mismatch.decision == "BLOCK"
    assert "sell-route quote does not match intended paper exit size" in mismatch.reasons
    unknown = replace(evidence(), missing_claims=("bundled_supply",))
    blocked = SolanaShieldPolicy().evaluate(
        MINT, unknown, safety(), signals(), open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
        sellability=route(), sell_amount_raw=10_000_000,
    )
    assert blocked.decision == "BLOCK"
    assert "mandatory token-risk claims remain unknown" in blocked.reasons
    live_unknown = replace(evidence(), missing_claims=("executable_sellability",))
    paper = SolanaShieldPolicy().evaluate(
        MINT, live_unknown, safety(), signals(), open_positions=0, amount=10, now_ms=10_000, sellability=route(), sell_amount_raw=10_000_000,
        sellability=route(), sell_amount_raw=10_000_000,
    )
    assert paper.decision == "APPROVE_PAPER_ENTRY"
    assert live_unknown.missing_claims == ("executable_sellability",)
