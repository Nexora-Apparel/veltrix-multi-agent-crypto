from dataclasses import dataclass
from .safety import SafetyReport

@dataclass
class Signal:
    agent: str
    token: str
    side: str
    reason: str

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
        if not address or not isinstance(report, SafetyReport) or not report.passed or report.token_address != address:
            return None
        return Signal("sniper", address, "BUY", "independent token safety evidence passed")

class Shield:
    def approve(self, signals, open_positions, amount, max_positions, max_amount):
        buys = [s for s in signals if s.side == "BUY"]
        distinct_agents = {s.agent for s in buys}
        same_token = len({s.token for s in buys}) == 1
        return same_token and len(distinct_agents) >= 2 and open_positions < max_positions and 0 < amount <= max_amount

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
