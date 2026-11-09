"""Polling coordinator for configured watchlists and profile-discovered candidates."""
from .discovery import DexScreenerDiscovery
from .ledger import PaperBroker
from .providers.dexscreener import DexScreenerProvider
from .signal_intelligence import SignalContextBuilder
import time

class MarketMonitor:
    def __init__(self, watchlist, db_path, provider=None, discovery=None, discovery_enabled=False, allowed_chains=("solana","base","bsc"), max_discovered_per_chain=20, broker=None, wallet_intelligence=None, wallet_max_age_ms=300_000):
        self.watchlist = watchlist
        self.provider = provider or DexScreenerProvider()
        self.discovery = discovery or DexScreenerDiscovery()
        self.discovery_enabled = discovery_enabled
        self.allowed_chains = tuple(allowed_chains)
        self.max_discovered_per_chain = max_discovered_per_chain
        self.ledger = broker or PaperBroker(db_path)
        self.signal_builder = SignalContextBuilder()
        self.signal_contexts = []
        self.wallet_intelligence = wallet_intelligence
        self.wallet_max_age_ms = wallet_max_age_ms
        self.last_wallet_intelligence = {}

    def poll_once(self):
        observations = []
        addresses_by_chain = {chain: list(addresses) for chain, addresses in self.watchlist.items()}
        if self.discovery_enabled:
            candidates = self.discovery.discover(self.allowed_chains)
            for candidate in candidates:
                self.ledger.record_discovery_candidate(candidate)
                chain_addresses = addresses_by_chain.setdefault(candidate.chain, [])
                if candidate.token_address not in chain_addresses and len(chain_addresses) < self.max_discovered_per_chain:
                    chain_addresses.append(candidate.token_address)
        for chain, addresses in addresses_by_chain.items():
            if not addresses:
                continue
            if self.wallet_intelligence is not None:
                now_ms = int(time.time() * 1000)
                since_ms = max(0, now_ms - self.wallet_max_age_ms)
                self.last_wallet_intelligence[chain] = self.wallet_intelligence.collect(
                    chain, addresses, since_ms, now_ms
                )
            events = self.provider.fetch_token_pairs(chain, addresses)
            for event in events:
                history = self.ledger.recent_market_observations(event.token_address, limit=20)
                context = self.signal_builder.build(event, history, int(time.time() * 1000))
                if context is not None:
                    self.signal_contexts.append(context)
                self.ledger.record_market_event(event)
                self.ledger.process_market_price(event.token_address, event.price_usd)
                observations.append(event)
        return observations
