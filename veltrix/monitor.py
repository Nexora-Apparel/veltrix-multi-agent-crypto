"""Polling coordinator for configured watchlists and profile-discovered candidates."""
from .discovery import DexScreenerDiscovery
from .ledger import PaperBroker
from .providers.dexscreener import DexScreenerProvider
from .signal_intelligence import SignalContextBuilder
from .liquidity_evidence import LiquidityEvidenceAnalyzer
import time

class MarketMonitor:
    def __init__(self, watchlist, db_path, provider=None, discovery=None, discovery_enabled=False, allowed_chains=("solana","base","bsc"), max_discovered_per_chain=20, broker=None, wallet_intelligence=None, wallet_max_age_ms=300_000, launch_intelligence=None, launch_origin_resolvers=None, trusted_launch_origin_providers=(), developer_evidence=None, liquidity_evidence_providers=None, liquidity_evidence_analyzer=None):
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
        self.developer_evidence = developer_evidence
        self.last_developer_results = {}
        self.liquidity_evidence_providers = liquidity_evidence_providers or {}
        self.liquidity_evidence_analyzer = liquidity_evidence_analyzer or LiquidityEvidenceAnalyzer()
        self.last_liquidity_results = {}
        self._launch_checked = set()
        self._launch_retry_after = {}

    def _evaluate_launch_candidate(self, candidate):
        if self.launch_intelligence is None:
            return
        key = (candidate.chain, candidate.token_address.lower()
               if candidate.chain in {"base", "bsc"} else candidate.token_address)
        if key in self._launch_checked:
            return
        now_ms = int(time.time() * 1000)
        if now_ms < self._launch_retry_after.get(key, 0):
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
            now_ms = int(time.time() * 1000)
            liquidity_evidence = None
            liquidity_provider = self.liquidity_evidence_providers.get(candidate.chain)
            if liquidity_provider is None:
                self.last_liquidity_results[key] = {
                    "status": "blocked", "reason": "chain-specific complete LP inventory reader is not configured"
                }
            else:
                try:
                    liquidity_batch = liquidity_provider.fetch_inventory(candidate.token_address)
                    liquidity_report = self.liquidity_evidence_analyzer.evaluate(
                        liquidity_batch, chain=candidate.chain,
                        token_address=candidate.token_address, now_ms=now_ms,
                    )
                    self.last_liquidity_results[key] = {
                        "status": "verified" if liquidity_report.passed else "blocked",
                        "pool_count": liquidity_report.pool_count,
                        "verified_pool_count": liquidity_report.verified_pool_count,
                        "active_locked_pool_count": liquidity_report.active_locked_pool_count,
                        "active_locked_pct": liquidity_report.active_locked_pct,
                        "earliest_unlock_at_ms": liquidity_report.earliest_unlock_at_ms,
                        "evidence_id": liquidity_report.evidence_id,
                        "reasons": liquidity_report.reasons,
                    }
                    if liquidity_report.passed:
                        liquidity_evidence = liquidity_report.to_safety_evidence(
                            independence_group=liquidity_batch.independence_group
                        )
                except Exception as exc:
                    self.last_liquidity_results[key] = {
                        "status": "blocked",
                        "reason": f"{type(exc).__name__}: chain liquidity evidence request failed",
                    }
            report, safety = self.launch_intelligence.evaluate(
                chain=candidate.chain,
                token_address=candidate.token_address,
                launch_at_ms=origin.launch_at_ms,
                now_ms=now_ms,
                liquidity_evidence=liquidity_evidence,
                developer_evidence=None,
                indexer_kwargs=kwargs,
            )
            self.last_launch_results[key] = {
                "status": "certified" if safety.passed and safety.composite_verified else "blocked",
                "origin_provider": origin.provider,
                "origin_evidence_id": origin.evidence_id,
                "liquidity_evidence_id": getattr(liquidity_evidence, "evidence_id", None),
                "liquidity_status": self.last_liquidity_results[key]["status"],
                "bundle_coverage_complete": report.coverage_complete,
                "bundled_supply_pct": report.bundled_supply_pct,
                "safety_reasons": safety.reasons,
            }
            if self.developer_evidence is not None:
                developer_result = self.developer_evidence.evaluate(
                    chain=candidate.chain,
                    token_address=candidate.token_address,
                    root_attestation=None,
                    liquidity_evidence=liquidity_evidence,
                    launch_bundle=report,
                    now_ms=now_ms,
                )
                self.last_developer_results[key] = {
                    "status": "analyzed" if developer_result.report.coverage_complete else "blocked",
                    "coverage_complete": developer_result.report.coverage_complete,
                    "developer_hold_pct": developer_result.report.dev_hold_pct,
                    "evidence_id": developer_result.report.evidence_id,
                    "root_provider": developer_result.report.root_provider,
                    "safety_passed": developer_result.safety_report.passed,
                    "reasons": developer_result.audit.get("reasons", ()),
                }
        except Exception as exc:
            self.last_launch_results[key] = {
                "status": "blocked", "reason": f"{type(exc).__name__}: {exc}"
            }
            self.ledger.record_launch_intelligence(candidate.chain, candidate.token_address,
                                                   self.last_launch_results[key])
            self._launch_retry_after[key] = int(time.time() * 1000) + 300_000
            return
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
