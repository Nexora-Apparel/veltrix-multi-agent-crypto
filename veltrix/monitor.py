"""Polling coordinator for configured watchlists and profile-discovered candidates."""
from .discovery import DexScreenerDiscovery
from .ledger import PaperBroker
from .providers.dexscreener import DexScreenerProvider
from .signal_intelligence import SignalContextBuilder
import time

class MarketMonitor:
    def __init__(self, watchlist, db_path, provider=None, discovery=None, discovery_enabled=False, allowed_chains=("solana","base","bsc"), max_discovered_per_chain=20, broker=None, wallet_intelligence=None, wallet_max_age_ms=300_000, launch_intelligence=None, launch_origin_resolvers=None, trusted_launch_origin_providers=()):
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
        self.launch_intelligence = launch_intelligence
        self.launch_origin_resolvers = launch_origin_resolvers or {}
        self.trusted_launch_origin_providers = frozenset(trusted_launch_origin_providers)
        self.last_launch_results = {}
        self._launch_checked = set()

    def _evaluate_launch_candidate(self, candidate):
        if self.launch_intelligence is None:
            return
        key = (candidate.chain, candidate.token_address.lower()
               if candidate.chain in {"base", "bsc"} else candidate.token_address)
        if key in self._launch_checked:
            return
        resolver = self.launch_origin_resolvers.get(candidate.chain)
        if resolver is None or getattr(resolver, "provider_id", None) not in self.trusted_launch_origin_providers:
            self.last_launch_results[key] = {
                "status": "blocked", "reason": "launch-origin resolver is missing or untrusted"
            }
            self.ledger.record_launch_intelligence(candidate.chain, candidate.token_address,
                                                   self.last_launch_results[key])
            self._launch_checked.add(key)
            return
        try:
            origin = resolver.resolve(
                candidate.token_address, self.launch_intelligence.launch_window_ms
            )
            if not origin.complete or origin.chain != candidate.chain:
                raise ValueError("launch-origin evidence is incomplete or chain-mismatched")
            kwargs = {}
            if candidate.chain in {"base", "bsc"}:
                if origin.launch_block is None or origin.window_end_block is None:
                    raise ValueError("EVM launch-origin block bounds are missing")
                kwargs = {
                    "launch_block": origin.launch_block,
                    "window_end_block": origin.window_end_block,
                }
            elif candidate.chain == "solana":
                if origin.launch_block is None:
                    raise ValueError("Solana launch slot is missing")
                kwargs = {"launch_slot": origin.launch_block}
            report, safety = self.launch_intelligence.evaluate(
                chain=candidate.chain,
                token_address=candidate.token_address,
                launch_at_ms=origin.launch_at_ms,
                now_ms=int(time.time() * 1000),
                liquidity_evidence=None,
                developer_evidence=None,
                indexer_kwargs=kwargs,
            )
            self.last_launch_results[key] = {
                "status": "certified" if safety.passed and safety.composite_verified else "blocked",
                "origin_provider": origin.provider,
                "origin_evidence_id": origin.evidence_id,
                "bundle_coverage_complete": report.coverage_complete,
                "bundled_supply_pct": report.bundled_supply_pct,
                "safety_reasons": safety.reasons,
            }
        except Exception as exc:
            self.last_launch_results[key] = {
                "status": "blocked", "reason": f"{type(exc).__name__}: {exc}"
            }
        self.ledger.record_launch_intelligence(candidate.chain, candidate.token_address,
                                               self.last_launch_results[key])
        self._launch_checked.add(key)

    def poll_once(self):
        observations = []
        addresses_by_chain = {chain: list(addresses) for chain, addresses in self.watchlist.items()}
        if self.discovery_enabled:
            candidates = self.discovery.discover(self.allowed_chains)
            for candidate in candidates:
                self.ledger.record_discovery_candidate(candidate)
                self._evaluate_launch_candidate(candidate)
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
