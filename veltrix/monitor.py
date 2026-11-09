"""Polling coordinator for explicitly configured token addresses."""
from .ledger import PaperBroker
from .providers.dexscreener import DexScreenerProvider

class MarketMonitor:
    def __init__(self, watchlist, db_path, provider=None):
        self.watchlist = watchlist
        self.provider = provider or DexScreenerProvider()
        self.ledger = PaperBroker(db_path)

    def poll_once(self):
        observations = []
        for chain, addresses in self.watchlist.items():
            if not addresses:
                continue
            events = self.provider.fetch_token_pairs(chain, addresses)
            for event in events:
                self.ledger.record_market_event(event)
                observations.append(event)
        return observations
