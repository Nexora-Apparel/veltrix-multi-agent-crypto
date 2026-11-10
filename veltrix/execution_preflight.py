"""Final evidence-composition gate before any future operator authorization.

A pass means only that the supplied, independently produced evidence satisfies
this policy. It does not certify a token as safe, authorize signing, or enable
transaction submission.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from solders.pubkey import Pubkey

from veltrix.semantic_transfer_decoder import SemanticDecodeResult, TOKEN_PROGRAM_ID
from veltrix.simulation_gate import SimulationGateResult
from veltrix.solana_meme.risk_evidence import RiskFlag, SolanaRiskEvidenceReport
from veltrix.transaction_validator import TransactionValidationResult


class ExecutionPreflightError(ValueError):
    """Raised when any required pre-sign evidence is absent or outside policy."""


REQUIRED_MINT_CLAIMS = (
    "mint_account_state",
    "mint_authority",
    "freeze_authority",
    "token_2022_extension_review",
    "complete_holder_inventory",
    "mint_holder_supply_reconciliation",
)


@dataclass(frozen=True, slots=True)
class ExecutionPreflightPolicy:
    expected_mint: str
    max_risk_evidence_age_ms: int = 300_000
    max_future_skew_ms: int = 1_000
    minimum_finalized_slot: int = 0
    max_top_holder_pct: float = 20.0
    max_top5_holder_pct: float = 50.0
    max_top10_holder_pct: float = 80.0
    blocked_flag_severities: tuple[str, ...] = ("critical", "high")

    def __post_init__(self) -> None:
        if not isinstance(self.expected_mint, str) or not self.expected_mint:
            raise ExecutionPreflightError("expected_mint is required")
        try:
            canonical = str(Pubkey.from_string(self.expected_mint))
        except Exception as exc:
            raise ExecutionPreflightError("expected_mint is not a valid Solana public key") from exc
        if canonical != self.expected_mint:
            raise ExecutionPreflightError("expected_mint must use canonical base58 encoding")
        for field in (
            "max_risk_evidence_age_ms",
            "max_future_skew_ms",
            "minimum_finalized_slot",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExecutionPreflightError(f"{field} must be a non-negative integer")
        if self.max_risk_evidence_age_ms == 0:
            raise ExecutionPreflightError("max_risk_evidence_age_ms must be positive")
        for field in ("max_top_holder_pct", "max_top5_holder_pct", "max_top10_holder_pct"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ExecutionPreflightError(f"{field} must be a finite percentage between 0 and 100")
        if not isinstance(self.blocked_flag_severities, tuple) or not self.blocked_flag_severities:
            raise ExecutionPreflightError("blocked_flag_severities must be a non-empty immutable tuple")
        if any(value not in {"critical", "high", "medium", "low", "info"} for value in self.blocked_flag_severities):
            raise ExecutionPreflightError("blocked_flag_severities contains an unsupported severity")


@dataclass(frozen=True, slots=True)
class ExecutionPreflightResult:
    preflight_passed: bool
    eligible_for_operator_authorization: bool
    message_sha256: str
    mint: str
    mint_evidence_id: str
    holder_evidence_id: str
    finalized_slot: int
    risk_evidence_observed_at_ms: int
    blocked_flag_count: int


def evaluate_execution_preflight(
    *,
    validation: TransactionValidationResult,
    semantic: SemanticDecodeResult,
    simulation: SimulationGateResult,
    risk_report: SolanaRiskEvidenceReport,
    policy: ExecutionPreflightPolicy,
    now_epoch_ms: int,
) -> ExecutionPreflightResult:
    """Compose independent structural, semantic, simulation and token-risk evidence."""
    if not isinstance(validation, TransactionValidationResult):
        raise ExecutionPreflightError("structural transaction validation is required")
    if not isinstance(semantic, SemanticDecodeResult):
        raise ExecutionPreflightError("semantic instruction validation is required")
    if not isinstance(simulation, SimulationGateResult) or simulation.approved is not True:
        raise ExecutionPreflightError("successful SimulationGate evidence is required")
    if not isinstance(risk_report, SolanaRiskEvidenceReport):
        raise ExecutionPreflightError("finalized token risk evidence report is required")
    if not isinstance(policy, ExecutionPreflightPolicy):
        raise ExecutionPreflightError("execution preflight policy is required")
    if isinstance(now_epoch_ms, bool) or not isinstance(now_epoch_ms, int) or now_epoch_ms <= 0:
        raise ExecutionPreflightError("now_epoch_ms must be a positive integer")

    digest = validation.message_sha256
    if semantic.message_sha256 != digest or simulation.message_sha256 != digest:
        raise ExecutionPreflightError("structural, semantic and simulation evidence message digests differ")
    if risk_report.mint != policy.expected_mint:
        raise ExecutionPreflightError("risk evidence mint does not match the approved mint")
    if not risk_report.mint_evidence_id or not risk_report.holder_evidence_id:
        raise ExecutionPreflightError("mint and holder evidence identifiers are both required")
    if risk_report.errors:
        raise ExecutionPreflightError("risk evidence report contains provider or parsing errors")
    if risk_report.observed_at_ms <= 0 or risk_report.observed_at_ms > now_epoch_ms + policy.max_future_skew_ms:
        raise ExecutionPreflightError("risk evidence timestamp is invalid or excessively future-dated")
    if now_epoch_ms - risk_report.observed_at_ms > policy.max_risk_evidence_age_ms:
        raise ExecutionPreflightError("risk evidence report is stale")
    if (
        isinstance(risk_report.mint_finalized_slot, bool)
        or not isinstance(risk_report.mint_finalized_slot, int)
        or risk_report.mint_finalized_slot < policy.minimum_finalized_slot
    ):
        raise ExecutionPreflightError("mint evidence does not meet the required finalized slot")

    missing = set(risk_report.missing_claims)
    if missing.intersection(REQUIRED_MINT_CLAIMS):
        raise ExecutionPreflightError("one or more required mint/holder evidence claims are missing")
    if risk_report.token_program_id != TOKEN_PROGRAM_ID:
        raise ExecutionPreflightError("only the classic SPL Token program is approved by this preflight policy")
    if risk_report.mint_authority is not None:
        raise ExecutionPreflightError("mint authority is still active")
    if risk_report.freeze_authority is not None:
        raise ExecutionPreflightError("freeze authority is still active")
    if risk_report.token_2022_extension_review_complete is not True or risk_report.token_2022_extension_types:
        raise ExecutionPreflightError("token extension review is incomplete or extensions are present")
    if risk_report.mint_supply_reconciled is not True:
        raise ExecutionPreflightError("mint supply does not reconcile with holder inventory")
    if (
        isinstance(risk_report.mint_supply_raw, bool)
        or not isinstance(risk_report.mint_supply_raw, int)
        or risk_report.mint_supply_raw <= 0
        or risk_report.total_supply_raw != risk_report.mint_supply_raw
        or risk_report.observed_holder_balance_raw != risk_report.mint_supply_raw
    ):
        raise ExecutionPreflightError("mint supply and complete holder balance totals do not match")
    if risk_report.holder_coverage_complete is not True:
        raise ExecutionPreflightError("complete holder inventory is required")
    if isinstance(risk_report.holder_count, bool) or not isinstance(risk_report.holder_count, int) or risk_report.holder_count <= 0:
        raise ExecutionPreflightError("holder inventory count is missing or invalid")

    for field, ceiling in (
        ("top_holder_pct", policy.max_top_holder_pct),
        ("top5_holder_pct", policy.max_top5_holder_pct),
        ("top10_holder_pct", policy.max_top10_holder_pct),
    ):
        value = getattr(risk_report, field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 100
        ):
            raise ExecutionPreflightError(f"{field} is missing or invalid")
        if value > ceiling:
            raise ExecutionPreflightError(f"{field} exceeds the configured concentration ceiling")

    if not isinstance(risk_report.risk_flags, tuple) or any(
        not isinstance(flag, RiskFlag) for flag in risk_report.risk_flags
    ):
        raise ExecutionPreflightError("risk flags are malformed")
    blocked = tuple(flag for flag in risk_report.risk_flags if flag.severity in policy.blocked_flag_severities)
    if blocked:
        raise ExecutionPreflightError("risk report contains blocked high-severity flags")

    if not semantic.token_transfers:
        raise ExecutionPreflightError("at least one approved classic SPL token transfer is required")
    if any(item.mint != policy.expected_mint for item in semantic.token_transfers):
        raise ExecutionPreflightError("semantic token transfer references an unexpected mint")

    return ExecutionPreflightResult(
        preflight_passed=True,
        eligible_for_operator_authorization=True,
        message_sha256=digest,
        mint=policy.expected_mint,
        mint_evidence_id=risk_report.mint_evidence_id,
        holder_evidence_id=risk_report.holder_evidence_id,
        finalized_slot=risk_report.mint_finalized_slot,
        risk_evidence_observed_at_ms=risk_report.observed_at_ms,
        blocked_flag_count=0,
    )
