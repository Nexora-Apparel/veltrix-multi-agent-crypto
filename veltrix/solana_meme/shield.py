"""Fail-closed paper-entry Shield for Solana meme candidates.

This gate can approve paper entries only. It has no wallet, signer, RPC write,
swap, or live-execution interface. Missing evidence is a veto, never a score.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from ..safety import SafetyReport
from .candidates import canonical_mint
from .risk_evidence import SolanaRiskEvidenceReport
from .sellability import SolanaSellabilityEvidence
from ..providers.solana_mint import TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID


@dataclass(frozen=True)
class ShieldDecision:
    mint: str
    decision: str
    reasons: tuple[str, ...]
    approving_agents: tuple[str, ...]
    amount: float
    observed_at_ms: int

    @property
    def approved_for_paper(self) -> bool:
        return self.decision == "APPROVE_PAPER_ENTRY"


class SolanaShieldPolicy:
    """Require complete evidence, composite safety, agent consensus and limits.

    Any unknown mandatory claim, missing provenance, or non-positive liquidity
    verification vetoes entry. Even a positive decision is strictly for the
    paper broker and never authorizes live execution.
    """

    def __init__(
        self,
        *,
        max_evidence_age_ms: int = 300_000,
        max_positions: int = 10,
        max_position_amount: float = 50.0,
        min_distinct_buy_agents: int = 2,
    ) -> None:
        if max_evidence_age_ms <= 0 or max_positions < 1:
            raise ValueError("invalid evidence age or position limit")
        if not math.isfinite(max_position_amount) or max_position_amount <= 0:
            raise ValueError("max_position_amount must be positive and finite")
        if min_distinct_buy_agents < 2:
            raise ValueError("at least two independent BUY agents are required")
        self.max_evidence_age_ms = max_evidence_age_ms
        self.max_positions = max_positions
        self.max_position_amount = max_position_amount
        self.min_distinct_buy_agents = min_distinct_buy_agents

    def evaluate(
        self,
        mint: str,
        evidence: SolanaRiskEvidenceReport,
        safety: SafetyReport | None,
        signals: Iterable[object],
        *,
        open_positions: int,
        amount: float,
        now_ms: int,
        sellability: SolanaSellabilityEvidence | None = None,
        sell_amount_raw: int | None = None,
    ) -> ShieldDecision:
        canonical = canonical_mint(mint)
        reasons: list[str] = []
        valid_now = isinstance(now_ms, int) and not isinstance(now_ms, bool) and now_ms >= 0
        evaluation_time = now_ms if valid_now else 0

        if not valid_now:
            reasons.append("evaluation timestamp is invalid")

        if not isinstance(evidence, SolanaRiskEvidenceReport):
            reasons.append("typed Solana risk evidence report is required")
        elif evidence.mint != canonical:
            reasons.append("risk evidence mint mismatch")
        else:
            provenance = (
                evidence.goplus_provider,
                evidence.goplus_evidence_id,
                evidence.holder_provider,
                evidence.holder_evidence_id,
                evidence.holder_block_ref,
            )
            if any(not isinstance(value, str) or not value.strip() for value in provenance):
                reasons.append("risk evidence provenance is incomplete")
            if (isinstance(evidence.goplus_observed_at_ms, bool)
                    or not isinstance(evidence.goplus_observed_at_ms, int)):
                reasons.append("GoPlus evidence timestamp is invalid")
            elif (evaluation_time - evidence.goplus_observed_at_ms < 0
                    or evaluation_time - evidence.goplus_observed_at_ms > self.max_evidence_age_ms):
                reasons.append("GoPlus evidence is stale or future-dated")
            if evidence.liquidity_locked is False:
                reasons.append("liquidity lock explicitly failed")
            elif evidence.liquidity_locked is not True:
                reasons.append("liquidity lock is not positively verified")
            if evidence.errors:
                reasons.append("risk evidence contains provider or validation errors")
            paper_risk_unknowns = tuple(
                claim for claim in evidence.missing_claims
                if claim != "executable_sellability"
            )
            if paper_risk_unknowns:
                reasons.append("mandatory token-risk claims remain unknown")
            if (isinstance(evidence.observed_at_ms, bool)
                    or not isinstance(evidence.observed_at_ms, int)):
                reasons.append("risk evidence report timestamp is invalid")
            else:
                age = evaluation_time - evidence.observed_at_ms
                if age < 0 or age > self.max_evidence_age_ms:
                    reasons.append("risk evidence report is stale or future-dated")
            if not evidence.holder_coverage_complete:
                reasons.append("complete reconciled holder inventory is required")
            if (not isinstance(evidence.mint_evidence_id, str)
                    or not evidence.mint_evidence_id.strip()
                    or isinstance(evidence.mint_finalized_slot, bool)
                    or not isinstance(evidence.mint_finalized_slot, int)
                    or evidence.mint_finalized_slot < 0
                    or evidence.token_program_id not in {TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID}):
                reasons.append("finalized mint-account provenance is incomplete")
            if evidence.mint_authority is not None:
                reasons.append("mint authority remains active")
            if evidence.freeze_authority is not None:
                reasons.append("freeze authority remains active")
            if evidence.token_2022_extension_types:
                reasons.append("Token-2022 extensions require separate policy certification")
            if evidence.token_2022_extension_review_complete is not True:
                reasons.append("Token-2022 extension review is incomplete")
            if (isinstance(evidence.total_supply_raw, bool)
                    or not isinstance(evidence.total_supply_raw, int)
                    or evidence.total_supply_raw <= 0
                    or isinstance(evidence.observed_holder_balance_raw, bool)
                    or evidence.observed_holder_balance_raw != evidence.total_supply_raw):
                reasons.append("holder balances do not reconcile exactly to supply")

        if not isinstance(safety, SafetyReport):
            reasons.append("typed composite safety report is required")
        else:
            if safety.token_address != canonical:
                reasons.append("composite safety report mint mismatch")
            if safety.passed is not True or safety.composite_verified is not True:
                reasons.append("composite safety certificate is not positively verified")
            if (isinstance(safety.observed_at_ms, bool)
                    or not isinstance(safety.observed_at_ms, int)):
                reasons.append("composite safety timestamp is invalid")
            else:
                age = evaluation_time - safety.observed_at_ms
                if age < 0 or age > self.max_evidence_age_ms:
                    reasons.append("composite safety certificate is stale or future-dated")

        # A paper entry requires a fresh sell route for the exact intended exit amount.
        # This is route evidence only; it never proves a transaction can execute.
        if not isinstance(sellability, SolanaSellabilityEvidence):
            reasons.append("typed sell-route evidence is required for paper entry")
        else:
            if sellability.mint != canonical:
                reasons.append("sell-route evidence mint mismatch")
            if sellability.state != "ROUTE_OBSERVED_NOT_EXECUTION_PROVEN":
                reasons.append("fresh sell-side route was not observed")
            if sellability.execution_proven is not False:
                reasons.append("sell-route evidence must not claim execution proof")
            if (isinstance(sellability.observed_at_ms, bool)
                    or not isinstance(sellability.observed_at_ms, int)):
                reasons.append("sell-route evidence timestamp is invalid")
            else:
                age = evaluation_time - sellability.observed_at_ms
                if age < 0 or age > self.max_evidence_age_ms:
                    reasons.append("sell-route evidence is stale or future-dated")
            if (not isinstance(sellability.provider, str) or not sellability.provider.strip()
                    or not isinstance(sellability.evidence_id, str) or not sellability.evidence_id.strip()):
                reasons.append("sell-route evidence provenance is incomplete")
            if (isinstance(sellability.quote_expires_at_ms, bool)
                    or not isinstance(sellability.quote_expires_at_ms, int)
                    or sellability.quote_expires_at_ms <= evaluation_time):
                reasons.append("sell-route quote is expired or has invalid expiry")
            if (isinstance(sell_amount_raw, bool)
                    or not isinstance(sell_amount_raw, int)
                    or sell_amount_raw <= 0
                    or sellability.input_amount_raw != sell_amount_raw):
                reasons.append("sell-route quote does not match intended paper exit size")
            if (isinstance(sellability.quoted_output_raw, bool)
                    or not isinstance(sellability.quoted_output_raw, int)
                    or sellability.quoted_output_raw <= 0
                    or isinstance(sellability.minimum_output_raw, bool)
                    or not isinstance(sellability.minimum_output_raw, int)
                    or sellability.minimum_output_raw <= 0):
                reasons.append("sell-route quote output is invalid")
            if (isinstance(sellability.route_plan_count, bool)
                    or not isinstance(sellability.route_plan_count, int)
                    or sellability.route_plan_count <= 0):
                reasons.append("sell-route quote contains no valid route plan")
            impact = sellability.price_impact_bps
            if (isinstance(impact, bool) or not isinstance(impact, (int, float))
                    or not math.isfinite(impact) or impact < 0 or impact > 300):
                reasons.append("sell-route price impact is invalid or above paper limit")
            fraction = sellability.input_supply_fraction
            if (isinstance(fraction, bool) or not isinstance(fraction, (int, float))
                    or not math.isfinite(fraction) or not 0.005 <= fraction <= 0.05):
                reasons.append("sell-route size fraction is outside paper bounds")
            elif (isinstance(sellability.input_amount_raw, int)
                    and isinstance(evidence, SolanaRiskEvidenceReport)
                    and isinstance(evidence.total_supply_raw, int)
                    and not isinstance(evidence.total_supply_raw, bool)
                    and evidence.total_supply_raw > 0
                    and not math.isclose(
                        fraction,
                        sellability.input_amount_raw / evidence.total_supply_raw,
                        rel_tol=1e-9,
                        abs_tol=1e-12,
                    )):
                reasons.append("sell-route size fraction does not reconcile to token supply")

        if isinstance(open_positions, bool) or not isinstance(open_positions, int) or open_positions < 0:
            reasons.append("open position count is invalid")
        elif open_positions >= self.max_positions:
            reasons.append("maximum concurrent paper positions reached")

        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount):
            reasons.append("paper position amount is invalid")
        elif amount <= 0 or amount > self.max_position_amount:
            reasons.append("paper position amount is outside configured limits")

        try:
            candidates = tuple(signals)
        except TypeError:
            candidates = ()
            reasons.append("agent signals are malformed")

        buy_agents: set[str] = set()
        buy_mints: set[str] = set()
        for signal in candidates:
            agent = getattr(signal, "agent", None)
            side = getattr(signal, "side", None)
            token = getattr(signal, "token", None)
            if side != "BUY":
                continue
            if not isinstance(agent, str) or not agent.strip():
                reasons.append("BUY signal has no valid agent identity")
                continue
            try:
                signal_mint = canonical_mint(token)
            except (TypeError, ValueError):
                reasons.append("BUY signal contains an invalid Solana mint")
                continue
            buy_agents.add(agent.strip().lower())
            buy_mints.add(signal_mint)

        if len(buy_agents) < self.min_distinct_buy_agents:
            reasons.append("insufficient distinct BUY-agent consensus")
        if buy_mints != {canonical}:
            reasons.append("BUY signals do not unanimously target this mint")

        unique_reasons = tuple(dict.fromkeys(reasons))
        decision = "BLOCK" if unique_reasons else "APPROVE_PAPER_ENTRY"
        valid_amount = isinstance(amount, (int, float)) and not isinstance(amount, bool) and math.isfinite(amount)
        return ShieldDecision(
            mint=canonical,
            decision=decision,
            reasons=unique_reasons,
            approving_agents=tuple(sorted(buy_agents)) if not unique_reasons else (),
            amount=float(amount) if valid_amount else 0.0,
            observed_at_ms=evaluation_time,
        )
