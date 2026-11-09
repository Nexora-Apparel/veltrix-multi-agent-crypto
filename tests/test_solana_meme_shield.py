from dataclasses import replace

from veltrix.agents import Signal
from veltrix.providers.solana_mint import TOKEN_PROGRAM_ID
from veltrix.safety import SafetyReport
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport
from veltrix.solana_meme.sellability import SolanaSellabilityEvidence
from veltrix.solana_meme.shield import SolanaShieldPolicy

MINT = "So11111111111111111111111111111111111111112"


def evidence():
    supply = 1_000_000_000
    return SolanaRiskEvidenceReport(
        mint=MINT, observed_at_ms=10_000,
        goplus_provider="goplus", goplus_evidence_id="gp:1",
        goplus_observed_at_ms=10_000, liquidity_locked=True,
        holder_provider="rpc", holder_evidence_id="holders:1",
        holder_block_ref="finalized:1", holder_count=2,
        total_supply_raw=supply, observed_holder_balance_raw=supply,
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
        state="ROUTE_OBSERVED_NOT_EXECUTION_PROVEN", errors=(),
        execution_proven=False,
    )
    values.update(changes)
    return SolanaSellabilityEvidence(**values)


def signals():
    return [
        Signal("scout", MINT, "BUY", "momentum"),
        Signal("whale", MINT, "BUY", "wallet activity"),
    ]


def evaluate(**kwargs):
    if "route" in kwargs:
        kwargs["sellability"] = kwargs.pop("route")
    defaults = dict(
        mint=MINT, evidence=evidence(), safety=safety(), signals=signals(),
        open_positions=0, amount=10, now_ms=10_000,
        sellability=route(), sell_amount_raw=10_000_000,
    )
    defaults.update(kwargs)
    return SolanaShieldPolicy().evaluate(**defaults)


def test_two_agent_consensus_can_approve_paper_only():
    result = evaluate(amount=50)
    assert result.decision == "APPROVE_PAPER_ENTRY"
    assert result.approved_for_paper is True
    assert result.approving_agents == ("scout", "whale")


def test_missing_route_blocks_paper_entry():
    result = evaluate(sellability=None, sell_amount_raw=None)
    assert result.decision == "BLOCK"
    assert "typed sell-route evidence is required for paper entry" in result.reasons


def test_route_observation_never_claims_execution_proof():
    result = evaluate()
    assert result.decision == "APPROVE_PAPER_ENTRY"
    assert route().state == "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN"
    assert route().execution_proven is False


def test_unknown_token_risk_claims_veto_even_with_route_and_safety():
    incomplete = replace(evidence(), missing_claims=("bundled_supply",))
    result = evaluate(evidence=incomplete)
    assert result.decision == "BLOCK"
    assert "mandatory token-risk claims remain unknown" in result.reasons


def test_live_execution_unknown_is_not_mistaken_for_token_risk_or_resolved():
    live_unknown = replace(evidence(), missing_claims=("executable_sellability",))
    result = evaluate(evidence=live_unknown)
    assert result.decision == "APPROVE_PAPER_ENTRY"
    assert live_unknown.missing_claims == ("executable_sellability",)


def test_wrong_exit_size_blocks():
    result = evaluate(route=route(input_amount_raw=9_000_000))
    assert result.decision == "BLOCK"
    assert "sell-route quote does not match intended paper exit size" in result.reasons


def test_route_size_fraction_must_reconcile_to_supply():
    result = evaluate(route=route(input_supply_fraction=0.02))
    assert result.decision == "BLOCK"
    assert "sell-route size fraction does not reconcile to token supply" in result.reasons


def test_stale_or_expired_route_blocks():
    stale = evaluate(now_ms=400_001, route=route(observed_at_ms=1_000, quote_expires_at_ms=500_000))
    assert stale.decision == "BLOCK"
    assert "sell-route evidence is stale or future-dated" in stale.reasons
    expired = evaluate(route=route(quote_expires_at_ms=9_999))
    assert expired.decision == "BLOCK"
    assert "sell-route quote is expired or has invalid expiry" in expired.reasons


def test_route_impact_and_size_bounds_are_enforced():
    high_impact = evaluate(route=route(price_impact_bps=301.0))
    assert high_impact.decision == "BLOCK"
    assert "sell-route price impact is invalid or above paper limit" in high_impact.reasons
    oversized_fraction = evaluate(route=route(input_supply_fraction=0.06))
    assert oversized_fraction.decision == "BLOCK"


def test_missing_or_unverified_composite_safety_vetoes():
    assert evaluate(safety=None).decision == "BLOCK"


def test_agent_consensus_and_mint_identity_are_required():
    one_agent = evaluate(signals=signals()[:1])
    assert one_agent.decision == "BLOCK"
    assert "insufficient distinct BUY-agent consensus" in one_agent.reasons
    other = "11111111111111111111111111111111"
    mixed = evaluate(signals=[
        Signal("scout", MINT, "BUY", "a"),
        Signal("whale", other, "BUY", "b"),
    ])
    assert mixed.decision == "BLOCK"
    assert "BUY signals do not unanimously target this mint" in mixed.reasons


def test_position_and_notional_limits_are_enforced():
    assert evaluate(open_positions=10).decision == "BLOCK"
    assert evaluate(amount=51).decision == "BLOCK"
    assert evaluate(amount=float("nan")).decision == "BLOCK"


def test_liquidity_provenance_and_authority_unknowns_veto():
    assert evaluate(evidence=replace(evidence(), liquidity_locked=None)).decision == "BLOCK"
    assert evaluate(evidence=replace(evidence(), holder_evidence_id="")).decision == "BLOCK"
    assert evaluate(evidence=replace(evidence(), mint_authority="11111111111111111111111111111111")).decision == "BLOCK"
    assert evaluate(evidence=replace(evidence(), freeze_authority="11111111111111111111111111111111")).decision == "BLOCK"


def test_token_2022_extension_requires_separate_certification():
    token_2022 = replace(
        evidence(),
        token_program_id="TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
        token_2022_extension_types=("permanent_delegate",),
        token_2022_extension_review_complete=False,
    )
    result = evaluate(evidence=token_2022)
    assert result.decision == "BLOCK"
    assert "Token-2022 extensions require separate policy certification" in result.reasons


def test_invalid_evaluation_timestamp_and_supply_are_rejected():
    assert evaluate(now_ms="invalid").decision == "BLOCK"
    invalid_supply = replace(evidence(), total_supply_raw=True, observed_holder_balance_raw=True)
    assert evaluate(evidence=invalid_supply).decision == "BLOCK"
