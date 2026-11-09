from dataclasses import dataclass
from .safety import SafetyReport

@dataclass
class Signal:
    agent: str
    token: str
    side: str
    reason: str

@dataclass(frozen=True)
class SolanaShieldDecision:
    mint: str
    state: str
    veto: bool
    entry_authorized: bool
    reasons: tuple[str, ...]

class Scout:
    def evaluate(self, token):
        address = token.get("address")
        if address and token.get("volume_spike") and token.get("price_change_5m", 0) > 0:
            return Signal("scout", address, "BUY", "positive 5m momentum and volume spike")

class Whale:
    def evaluate(self, token):
        address = token.get("address")
        wallets = set(token.get("smart_wallet_buys", []))
        if address and len(wallets) >= 2:
            return Signal("whale", address, "BUY", f"{len(wallets)} distinct smart wallets buying")

class Sniper:
    def evaluate(self, token):
        address = token.get("address")
        report = token.get("safety_report")
        if not address or not isinstance(report, SafetyReport) or not report.passed or not report.composite_verified or report.token_address != address:
            return None
        return Signal("sniper", address, "BUY", "three-source composite token safety gate passed")

class Shield:
    def approve(self, signals, open_positions, amount, max_positions, max_amount):
        buys = [s for s in signals if s.side == "BUY"]
        distinct_agents = {s.agent for s in buys}
        same_token = len({s.token for s in buys}) == 1
        return same_token and len(distinct_agents) >= 2 and open_positions < max_positions and 0 < amount <= max_amount

    def evaluate_solana_evidence(self, report):
        """Apply the Solana evidence veto before agent consensus.

        This method never authorizes an entry. A complete evidence packet only
        advances a mint to independent-agent review; a later paper-trading
        policy must still enforce consensus, sizing, and position limits.
        """
        from .solana_meme.risk_evidence import SolanaRiskEvidenceReport

        if not isinstance(report, SolanaRiskEvidenceReport):
            return SolanaShieldDecision(
                mint="", state="BLOCK", veto=True, entry_authorized=False,
                reasons=("missing_or_untyped_solana_risk_evidence",),
            )

        reasons = []
        if report.liquidity_locked is False:
            return SolanaShieldDecision(
                mint=report.mint, state="BLOCK", veto=True, entry_authorized=False,
                reasons=("liquidity_lock_explicitly_not_verified",),
            )
        if report.liquidity_locked is None:
            reasons.append("liquidity_lock_unknown")
        if not report.holder_coverage_complete:
            reasons.append("holder_inventory_incomplete_or_unreconciled")
        if report.missing_claims:
            reasons.extend(f"missing_claim:{claim}" for claim in report.missing_claims)
        if report.errors:
            reasons.extend(f"evidence_error:{error}" for error in report.errors)
        if reasons:
            return SolanaShieldDecision(
                mint=report.mint, state="HOLD", veto=True, entry_authorized=False,
                reasons=tuple(reasons),
            )
        return SolanaShieldDecision(
            mint=report.mint, state="READY_FOR_AGENT_CONSENSUS", veto=False,
            entry_authorized=False,
            reasons=("evidence_complete_but_not_an_entry_authorization",),
        )

class Exit:
    def decisions(self, entry, current, remaining_fraction=1.0):
        if entry <= 0 or current <= 0 or not 0 < remaining_fraction <= 1:
            return []
        multiple = current / entry
        if multiple <= 0.94:
            return [("stop_loss", remaining_fraction)]
        if multiple >= 3:
            return [("take_3x_and_trail", remaining_fraction * 0.5)]
        if multiple >= 2:
            return [("take_2x", remaining_fraction * 0.5)]
        return []
