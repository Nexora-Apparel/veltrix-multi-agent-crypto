"""Consensus-to-paper execution coordinator. It cannot place live orders."""
from .agents import Scout, Whale, Sniper, Shield

class Core:
    def __init__(self, broker, max_positions=10, max_position_usd=50, agents=None):
        if broker is None:
            raise ValueError("A durable PaperBroker is required")
        self.broker = broker
        self.agents = tuple(agents) if agents is not None else (Scout(), Whale(), Sniper())
        self.shield = Shield()
        self.max_positions = max_positions
        self.max_position_usd = max_position_usd

    def evaluate(self, token, amount_usd=None):
        """Evaluate one normalized token snapshot and open only on same-token consensus."""
        address = token.get("address")
        try:
            price = float(token.get("price", 0))
        except (TypeError, ValueError):
            price = 0
        try:
            amount = self.max_position_usd if amount_usd is None else float(amount_usd)
        except (TypeError, ValueError):
            amount = 0
        if not address or price <= 0:
            self.broker.record_agent_decision(address or "", "REJECT", [], "missing token address or valid price")
            return None
        signals = [signal for agent in self.agents if (signal := agent.evaluate(token))]
        # Multiple instances or malformed agents must not manufacture consensus.
        signals = [s for s in signals if getattr(s, "agent", None) and
                   getattr(s, "token", None) and getattr(s, "side", None) in {"BUY", "SELL"}]
        snapshot = self.broker.snapshot()
        open_count = len(snapshot["open_positions"])
        approved = self.shield.approve(
            signals, open_count, amount, self.max_positions, self.max_position_usd
        )
        reasons = [f"{s.agent}: {s.reason}" for s in signals]
        if not approved:
            reason = "consensus or risk gate rejected entry"
            self.broker.record_agent_decision(address, "REJECT", reasons, reason)
            return None
        try:
            position = self.broker.open_position(address, price, amount, "; ".join(reasons))
        except (ValueError, sqlite3.Error) as exc:
            self.broker.record_agent_decision(address, "REJECT", reasons, str(exc))
            return None
        result = {"token": address, "side": "BUY", "price": price, "amount_usd": amount,
                  "quantity": position["quantity"], "signals": reasons}
        self.broker.record_agent_decision(address, "BUY", reasons, "same-token multi-agent consensus approved")
        return result

import sqlite3
