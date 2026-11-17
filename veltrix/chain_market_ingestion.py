"""Bounded read-only multi-chain market observation ingestion."""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .database import connect_database
from .discovery import DexScreenerDiscovery
from .providers.dexscreener import DexScreenerProvider
from .runtime_telemetry import RuntimeTelemetryStore


CHAINS = ("solana", "base", "bsc")


class ChainMarketObservationCollector:
    """Collect DexScreener snapshots and append only market observation events."""

    def __init__(
        self, database_path: str, *, provider: Any | None = None,
        discovery: Any | None = None, clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.database_path = database_path
        self.provider = provider or DexScreenerProvider()
        self.discovery = discovery or DexScreenerDiscovery()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.telemetry = RuntimeTelemetryStore(database_path)

    def run_once(
        self, tokens_by_chain: dict[str, list[str]], *,
        discover_latest_profiles: bool = False, limit_per_chain: int = 25,
    ) -> dict[str, Any]:
        if isinstance(limit_per_chain, bool) or not isinstance(limit_per_chain, int) or not 1 <= limit_per_chain <= 25:
            raise ValueError("limit_per_chain must be between 1 and 25")
        if not isinstance(tokens_by_chain, dict) or set(tokens_by_chain) - set(CHAINS):
            raise ValueError("tokens_by_chain contains an unsupported chain")
        tokens = {chain: list(dict.fromkeys(
            value.strip() for value in tokens_by_chain.get(chain, [])
            if isinstance(value, str) and value.strip()
        )) for chain in CHAINS}
        if discover_latest_profiles:
            for candidate in self.discovery.discover(CHAINS):
                if candidate.chain in tokens and candidate.token_address not in tokens[candidate.chain]:
                    tokens[candidate.chain].append(candidate.token_address)
        active_chains = [chain for chain in CHAINS if tokens[chain]]
        if not active_chains:
            raise ValueError("at least one chain token or profile-discovery candidate is required")

        inserted = {chain: 0 for chain in CHAINS}
        failures = {}
        pending_events: list[tuple[str, str, float]] = []

        # Do all provider/discovery I/O before opening the write connection.
        # Turso's serverless transaction stream can expire while idle; keeping
        # a remote connection open during HTTP requests caused the later INSERT
        # to fail with "interactive transaction ... idle for too long".
        for chain in active_chains:
            addresses = tokens[chain][:limit_per_chain]
            observed_at = self.clock_ms()
            started = time.monotonic()
            try:
                events = self.provider.fetch_token_pairs(chain, addresses)
            except Exception as exc:
                self.telemetry.record_provider_probe(
                    "dexscreener", chain, succeeded=False,
                    observed_at_ms=observed_at,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error_code=type(exc).__name__,
                )
                failures[chain] = type(exc).__name__
                continue
            self.telemetry.record_provider_probe(
                "dexscreener", chain, succeeded=True,
                observed_at_ms=observed_at,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            for event in events:
                if event.chain != chain or event.token_address not in addresses:
                    continue
                payload = {
                    "chain": event.chain,
                    "token": event.token_address,
                    "pair": event.pair_address,
                    "symbol": event.symbol,
                    "price_usd": event.price_usd,
                    "liquidity_usd": event.liquidity_usd,
                    "volume_h1_usd": event.volume_h1_usd,
                    "price_change_m5_pct": event.price_change_m5_pct,
                    "observed_at_ms": event.observed_at_ms,
                    "source": event.source,
                }
                pending_events.append((
                    "MARKET_OBSERVATION",
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    event.observed_at_ms / 1000,
                ))
                inserted[chain] += 1

        db = connect_database(self.database_path)
        try:
            for row in pending_events:
                db.execute(
                    "INSERT INTO events(event,payload,created_at) VALUES(?,?,?)",
                    row,
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return {
            "status": "PARTIAL" if failures else "SUCCEEDED",
            "chains": active_chains,
            "observations_inserted": inserted,
            "provider_failures": failures,
            "paper_trading_enabled": False,
            "live_execution_enabled": False,
        }
