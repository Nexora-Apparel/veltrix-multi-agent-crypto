from dataclasses import dataclass

@dataclass
class Signal:
    agent: str
    token: str
    side: str
    reason: str

class Scout:
    def evaluate(self, token):
        if token.get("volume_spike") and token.get("price_change_5m", 0) > 0:
            return Signal("scout", token["address"], "BUY", "positive 5m momentum and volume spike")

class Whale:
    def evaluate(self, token):
        wallets = set(token.get("smart_wallet_buys", []))
        if len(wallets) >= 2:
            return Signal("whale", token["address"], "BUY", f"{len(wallets)} distinct smart wallets buying")

class Sniper:
    def evaluate(self, token):
        required = ("liquidity_locked", "dev_hold_pct", "bundled_supply_pct")
        if any(k not in token for k in required): return None
        if token["liquidity_locked"] is True and token["dev_hold_pct"] < 5 and token["bundled_supply_pct"] == 0:
            return Signal("sniper", token["address"], "BUY", "explicit launch safety fields passed")

class Shield:
    def approve(self, signals, open_positions, amount, max_positions, max_amount):
        return len({s.agent for s in signals if s.side == "BUY"}) >= 2 and open_positions < max_positions and 0 < amount <= max_amount

class Exit:
    def decisions(self, entry, current, remaining_fraction=1.0):
        if entry <= 0 or current <= 0: return []
        multiple = current / entry
        if multiple >= 3 and remaining_fraction > 0: return [("take_3x_and_trail", remaining_fraction * 0.5)]
        if multiple >= 2 and remaining_fraction > 0: return [("take_2x", remaining_fraction * 0.5)]
        if multiple <= 0.94: return [("stop_loss", remaining_fraction)]
        return []
