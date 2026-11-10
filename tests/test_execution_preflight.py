"""Adversarial tests for the final evidence-composition preflight gate."""
import dataclasses
import pytest
from solders.pubkey import Pubkey

from veltrix.execution_preflight import (
    ExecutionPreflightError,
    ExecutionPreflightPolicy,
    REQUIRED_LIVE_RISK_CLAIMS,
    evaluate_execution_preflight,
)
from veltrix.semantic_transfer_decoder import DecodedTokenTransfer, SemanticDecodeResult, TOKEN_PROGRAM_ID
from veltrix.simulation_gate import SimulationGateResult
from veltrix.solana_meme.risk_evidence import RiskFlag, SolanaRiskEvidenceReport
from veltrix.transaction_validator import TransactionValidationResult


def fixture():
    mint = str(Pubkey.new_unique())
    digest = "a" * 64
    validation = TransactionValidationResult(
        message_sha256=digest,
        message_size=128,
        fee_payer="fee-payer",
        required_signers=("fee-payer",),
        program_ids=(TOKEN_PROGRAM_ID,),
        instruction_count=1,
    )
    semantic = SemanticDecodeResult(
        message_sha256=digest,
        token_transfers=(
            DecodedTokenTransfer(
                source="source-account",
                mint=mint,
                destination="destination-account",
                authority="fee-payer",
                amount_raw=100,
                decimals=6,
            ),
        ),
        native_transfers=(),
        compute_unit_limit=300_000,
        compute_unit_price_micro_lamports=5_000,
        total_native_lamports=0,
        token_amounts_by_mint=((mint, 100),),
    )
    simulation = SimulationGateResult(
        approved=True,
        message_sha256=digest,
        provider_id="rpc-primary",
        response_id="sim-1",
        context_slot=500,
        units_consumed=100_000,
        fee_lamports=5_000,
        verified_token_account_count=2,
    )
    risk = SolanaRiskEvidenceReport(
        mint=mint,
        observed_at_ms=1_000_000,
        goplus_provider=None,
        goplus_evidence_id=None,
        goplus_observed_at_ms=None,
        liquidity_locked=None,
        goplus_dev_hold_pct=None,
        goplus_bundled_supply_pct=None,
        goplus_creator_hold_pct=None,
        holder_provider="holder-provider",
        holder_evidence_id="holder-evidence-1",
        holder_block_ref="finalized-slot-500",
        holder_count=100,
        total_supply_raw=1_000,
        observed_holder_balance_raw=1_000,
        holder_coverage_complete=True,
        top_holder_pct=10.0,
        top5_holder_pct=30.0,
        top10_holder_pct=60.0,
        missing_claims=(),
        errors=(),
        mint_evidence_id="mint-evidence-1",
        mint_finalized_slot=500,
        token_program_id=TOKEN_PROGRAM_ID,
        mint_authority=None,
        freeze_authority=None,
        token_2022_extension_types=(),
        token_2022_extension_review_complete=True,
        mint_supply_raw=1_000,
        mint_decimals=6,
        mint_supply_reconciled=True,
        risk_flags=(),
    )
    policy = ExecutionPreflightPolicy(
        expected_mint=mint,
        minimum_finalized_slot=490,
    )
    return validation, semantic, simulation, risk, policy


def evaluate(values=None):
    validation, semantic, simulation, risk, policy = values or fixture()
    return evaluate_execution_preflight(
        validation=validation,
        semantic=semantic,
        simulation=simulation,
        risk_report=risk,
        policy=policy,
        now_epoch_ms=1_005_000,
    )


def test_passes_only_when_all_four_evidence_domains_match():
    result = evaluate()
    assert result.preflight_passed is True
    assert result.eligible_for_operator_authorization is True
    assert result.blocked_flag_count == 0


def test_rejects_message_digest_mismatch_between_gates():
    validation, semantic, simulation, risk, policy = fixture()
    semantic = dataclasses.replace(semantic, message_sha256="b" * 64)
    with pytest.raises(ExecutionPreflightError, match="digests differ"):
        evaluate((validation, semantic, simulation, risk, policy))


def test_rejects_unapproved_mint():
    validation, semantic, simulation, risk, policy = fixture()
    other = str(Pubkey.new_unique())
    policy = dataclasses.replace(policy, expected_mint=other)
    with pytest.raises(ExecutionPreflightError, match="does not match"):
        evaluate((validation, semantic, simulation, risk, policy))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("mint_authority", "authority-key", "mint authority"),
        ("freeze_authority", "freeze-key", "freeze authority"),
        ("token_program_id", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb", "classic SPL Token"),
        ("token_2022_extension_review_complete", False, "extension review"),
        ("token_2022_extension_types", ("transfer_hook",), "extension review"),
        ("mint_supply_reconciled", False, "does not reconcile"),
        ("holder_coverage_complete", False, "complete holder inventory"),
        ("top_holder_pct", 20.01, "top_holder_pct"),
        ("top5_holder_pct", 50.01, "top5_holder_pct"),
        ("top10_holder_pct", 80.01, "top10_holder_pct"),
        ("observed_at_ms", 600_000, "stale"),
        ("mint_finalized_slot", 489, "finalized slot"),
        ("holder_count", 0, "holder inventory count"),
        ("mint_decimals", 39, "mint decimals"),
    ],
)
def test_rejects_unsafe_or_incomplete_risk_report_fields(field, value, message):
    validation, semantic, simulation, risk, policy = fixture()
    risk = dataclasses.replace(risk, **{field: value})
    with pytest.raises(ExecutionPreflightError, match=message):
        evaluate((validation, semantic, simulation, risk, policy))


@pytest.mark.parametrize("claim", REQUIRED_LIVE_RISK_CLAIMS)
def test_rejects_every_missing_required_live_risk_claim(claim):
    validation, semantic, simulation, risk, policy = fixture()
    risk = dataclasses.replace(risk, missing_claims=(claim,))
    with pytest.raises(ExecutionPreflightError, match="required live risk evidence claims"):
        evaluate((validation, semantic, simulation, risk, policy))


def test_rejects_high_or_critical_risk_flags():
    validation, semantic, simulation, risk, policy = fixture()
    risk = dataclasses.replace(
        risk,
        risk_flags=(RiskFlag("freeze-risk", "critical", "Freeze authority risk", "mint-provider"),),
    )
    with pytest.raises(ExecutionPreflightError, match="high-severity flags"):
        evaluate((validation, semantic, simulation, risk, policy))


def test_rejects_risk_provider_errors():
    validation, semantic, simulation, risk, policy = fixture()
    risk = dataclasses.replace(risk, errors=("holder provider timed out",))
    with pytest.raises(ExecutionPreflightError, match="provider or parsing errors"):
        evaluate((validation, semantic, simulation, risk, policy))


def test_rejects_unapproved_token_mint_in_semantic_effects():
    validation, semantic, simulation, risk, policy = fixture()
    transfer = dataclasses.replace(semantic.token_transfers[0], mint=str(Pubkey.new_unique()))
    semantic = dataclasses.replace(semantic, token_transfers=(transfer,))
    with pytest.raises(ExecutionPreflightError, match="unexpected mint"):
        evaluate((validation, semantic, simulation, risk, policy))


def test_rejects_missing_semantic_token_transfer():
    validation, semantic, simulation, risk, policy = fixture()
    semantic = dataclasses.replace(semantic, token_transfers=())
    with pytest.raises(ExecutionPreflightError, match="at least one"):
        evaluate((validation, semantic, simulation, risk, policy))


def test_rejects_simulation_not_approved():
    validation, semantic, simulation, risk, policy = fixture()
    simulation = dataclasses.replace(simulation, approved=False)
    with pytest.raises(ExecutionPreflightError, match="successful SimulationGate"):
        evaluate((validation, semantic, simulation, risk, policy))


def test_rejects_missing_evidence_identifiers():
    validation, semantic, simulation, risk, policy = fixture()
    risk = dataclasses.replace(risk, mint_evidence_id=None)
    with pytest.raises(ExecutionPreflightError, match="evidence identifiers"):
        evaluate((validation, semantic, simulation, risk, policy))


def test_policy_rejects_invalid_required_claims():
    with pytest.raises(ExecutionPreflightError, match="required_claims"):
        ExecutionPreflightPolicy(expected_mint=str(Pubkey.new_unique()), required_claims=())
