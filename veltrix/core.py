"""Consensus-to-paper execution coordinator. It cannot place live orders."""
import math
import sqlite3
import time

from .agents import Scout, Whale, Sniper, Shield
from .solana_meme.shield import SolanaShieldPolicy


class Core:
    def __init__(self, broker, max_positions=10, max_position_usd=50, agents=None):
        if broker is None:
            raise ValueError("A durable PaperBroker is required")
        self.broker = broker
        self.agents = tuple(agents) if agents is not None else (Scout(), Whale(), Sniper())
        self.shield = Shield()
        self.solana_shield = SolanaShieldPolicy(
            max_positions=max_positions,
            max_position_amount=max_position_usd,
        )
        self.max_positions = max_positions
        self.max_position_usd = max_position_usd

    def evaluate_with_intelligence(self, token, intelligence_result, amount_usd=None):
        """Evaluate using only matching coordinator evidence for Whale signals."""
        from .providers.coordinator import (
            CrossChainIntelligenceCoordinator, CrossChainIntelligenceResult,
        )

        clean = dict(token)
        clean.pop("smart_wallet_buys", None)
        clean.pop("whale_cluster_count", None)
        clean.pop("whale_cluster_notional_usd", None)
        chain = str(clean.get("chain", "")).lower()
        address = clean.get("address")
        if (isinstance(intelligence_result, CrossChainIntelligenceResult)
                and chain == intelligence_result.chain and isinstance(address, str)):
            clean = CrossChainIntelligenceCoordinator.enrich_token(
                clean, intelligence_result
            )
        else:
            clean["smart_wallet_buys"] = []
            clean["whale_cluster_count"] = 0
            clean["whale_cluster_notional_usd"] = 0.0
        return self.evaluate(clean, amount_usd)

    def evaluate(self, token, amount_usd=None):
        """Evaluate one normalized token snapshot and fail closed on Solana."""
        address = token.get("address")
        try:
            price = float(token.get("price", 0))
        except (TypeError, ValueError):
            price = 0.0
        try:
            amount = self.max_position_usd if amount_usd is None else float(amount_usd)
        except (TypeError, ValueError):
            amount = 0.0
        if (not isinstance(address, str) or not address.strip()
                or not math.isfinite(price) or price <= 0
                or not math.isfinite(amount) or amount <= 0):
            self.broker.record_agent_decision(
                address or "", "REJECT", [], "missing token address or invalid price/amount"
            )
            return None

        signals = [signal for agent in self.agents if (signal := agent.evaluate(token))]
        # Multiple instances or malformed agents must not manufacture consensus.
        signals = [
            s for s in signals
            if getattr(s, "agent", None) and getattr(s, "token", None)
            and getattr(s, "side", None) in {"BUY", "SELL"}
        ]
        snapshot = self.broker.snapshot()
        open_count = len(snapshot["open_positions"])
        reasons = [f"{s.agent}: {s.reason}" for s in signals]

        chain = str(token.get("chain", "")).lower()
        # A valid Solana public key must not bypass the Solana Shield by
        # omitting its chain label or by being mislabeled as an EVM asset.
        from .solana_meme.candidates import canonical_mint
        try:
            canonical_mint(address)
            is_solana_mint = True
        except (TypeError, ValueError):
            is_solana_mint = False
        if is_solana_mint and chain not in {"", "solana"}:
            self.broker.record_agent_decision(
                address, "REJECT", reasons, "Solana public key has a conflicting chain label"
            )
            return None
        if is_solana_mint:
            chain = "solana"
        if chain == "solana":
            try:
                decision = self.solana_shield.evaluate(
                    address,
                    token.get("risk_evidence_report"),
                    token.get("safety_report"),
                    signals,
                    open_positions=open_count,
                    amount=amount,
                    now_ms=int(time.time() * 1000),
                    sellability=token.get("sellability_evidence"),
                    sell_amount_raw=token.get("sell_amount_raw"),
                    token_price_usd=price,
                )
            except (TypeError, ValueError) as exc:
                self.broker.record_agent_decision(
                    address, "REJECT", reasons, f"Solana Shield rejected malformed candidate: {exc}"
                )
                return None
            if not decision.approved_for_paper:
                reason = "; ".join(decision.reasons) or "Solana Shield veto"
                self.broker.record_agent_decision(
                    address, "REJECT", reasons, f"Solana Shield: {reason}"
                )
                return None
            route_evidence = token.get("sellability_evidence")
            reasons.append("Solana Shield approved paper-only entry")
            reasons.append(
                "Paper exit evidence: ROUTE_OBSERVED_NOT_EXECUTION_PROVEN "
                f"({route_evidence.evidence_id}; raw_amount={route_evidence.input_amount_raw})"
            )
        else:
            approved = self.shield.approve(
                signals, open_count, amount, self.max_positions, self.max_position_usd
            )
            if not approved:
                self.broker.record_agent_decision(
                    address, "REJECT", reasons, "consensus or risk gate rejected entry"
                )
                return None

        try:
            position = self.broker.open_position(address, price, amount, "; ".join(reasons))
        except (ValueError, sqlite3.Error) as exc:
            self.broker.record_agent_decision(address, "REJECT", reasons, str(exc))
            return None
        result = {
            "token": address, "side": "BUY", "price": price, "amount_usd": amount,
            "quantity": position["quantity"], "signals": reasons,
            "sellability_state": (
                token["sellability_evidence"].state if chain == "solana" else None
            ),
            "sellability_evidence_id": (
                token["sellability_evidence"].evidence_id if chain == "solana" else None
            ),
            "sell_amount_raw": (
                token["sell_amount_raw"] if chain == "solana" else None
            ),
        }
        self.broker.record_agent_decision(
            address, "BUY", reasons, "same-token paper Shield and multi-agent consensus approved"
        )
        return result
