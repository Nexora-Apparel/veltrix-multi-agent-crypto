from .agents import Scout, Whale, Sniper, Shield

class Core:
    def __init__(self, max_positions=10, max_position_usd=50):
        self.agents = (Scout(), Whale(), Sniper())
        self.shield = Shield()
        self.max_positions, self.max_position_usd = max_positions, max_position_usd
        self.positions = {}

    def evaluate(self, token):
        signals = [s for a in self.agents if (s := a.evaluate(token))]
        if self.shield.approve(signals, len(self.positions), self.max_position_usd, self.max_positions, self.max_position_usd):
            address = token["address"]
            self.positions[address] = {"entry": float(token["price"]), "usd": self.max_position_usd}
            return {"token": address, "side": "BUY", "reason": [s.reason for s in signals]}
        return None
