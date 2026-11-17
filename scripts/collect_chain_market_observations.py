"""Manual one-shot read-only market observation collector for Solana, Base and BSC.

Writes only normalized MARKET_OBSERVATION rows to the existing events ledger.
It does not construct PaperBroker, run position management, or execute trades.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from veltrix.database import connect_database, is_remote_database
from veltrix.discovery import DexScreenerDiscovery
from veltrix.foundation import validate_runtime_controls
from veltrix.providers.dexscreener import DexScreenerProvider
from veltrix.runtime_telemetry import RuntimeTelemetryStore


CHAINS = ("solana", "base", "bsc")


def _addresses(name: str) -> list[str]:
    values = [value.strip() for value in os.getenv(name, "").split(",") if value.strip()]
    return list(dict.fromkeys(values))


def main() -> None:
    validate_runtime_controls()
    url = os.getenv("TURSO_DATABASE_URL", "").strip()
    token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token or not is_remote_database(url):
        raise SystemExit("explicit production Turso URL and write token are required")
    parsed = urlsplit(url)
    if parsed.scheme != "libsql" or not (parsed.hostname or "").endswith(".turso.io"):
        raise SystemExit("TURSO_DATABASE_URL must be a credential-free libsql://*.turso.io URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise SystemExit("TURSO_DATABASE_URL must not contain credentials or URL extras")
    if parsed.hostname == "veltrix-h0-conformance-test-nexora-apparel.aws-eu-central-1.turso.io":
        raise SystemExit("refusing to write to the disposable conformance database")
    if os.getenv("LIVE_TRADING", "false").lower() not in {"false", "0", "no", "off", ""}:
        raise SystemExit("LIVE_TRADING must remain false")
    if os.getenv("PAPER_TRADING_ENABLED", "false").lower() not in {"false", "0", "no", "off", ""}:
        raise SystemExit("PAPER_TRADING_ENABLED must remain false")

    tokens = {
        chain: _addresses(f"VELTRIX_{chain.upper()}_MARKET_TOKENS")
        for chain in CHAINS
    }
    discovery_enabled = os.getenv("VELTRIX_DISCOVER_LATEST_PROFILES", "false").lower() in {"1", "true", "yes", "on"}
    chains = tuple(chain for chain in CHAINS if tokens[chain])
    discovery = DexScreenerDiscovery()
    if discovery_enabled:
        candidates = discovery.discover(CHAINS)
        for item in candidates:
            if item.chain in tokens and item.token_address not in tokens[item.chain]:
                tokens[item.chain].append(item.token_address)
        chains = CHAINS
    limit = int(os.getenv("VELTRIX_CHAIN_MARKET_LIMIT_PER_CHAIN", "25"))
    if not 1 <= limit <= 25:
        raise SystemExit("VELTRIX_CHAIN_MARKET_LIMIT_PER_CHAIN must be between 1 and 25")
    if not any(tokens.values()):
        raise SystemExit("configure a chain token watchlist or enable latest-profile discovery")

    telemetry = RuntimeTelemetryStore(url)
    provider = DexScreenerProvider()
    db = connect_database(url)
    inserted = {chain: 0 for chain in CHAINS}
    failures = {}
    try:
        for chain in chains:
            addresses = tokens[chain][:limit]
            if not addresses:
                continue
            import time
            started = time.monotonic()
            observed_at = int(time.time() * 1000)
            try:
                events = provider.fetch_token_pairs(chain, addresses)
            except Exception as exc:
                latency = int((time.monotonic() - started) * 1000)
                telemetry.record_provider_probe(
                    "dexscreener", chain, succeeded=False, observed_at_ms=observed_at,
                    latency_ms=latency, error_code=type(exc).__name__,
                )
                failures[chain] = type(exc).__name__
                continue
            latency = int((time.monotonic() - started) * 1000)
            telemetry.record_provider_probe(
                "dexscreener", chain, succeeded=True, observed_at_ms=observed_at,
                latency_ms=latency,
            )
            for event in events:
                if event.chain != chain:
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
                db.execute(
                    "INSERT INTO events(event,payload,created_at) VALUES(?,?,?)",
                    ("MARKET_OBSERVATION", json.dumps(payload, sort_keys=True), event.observed_at_ms / 1000),
                )
                inserted[chain] += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    print(json.dumps({
        "status": "PARTIAL" if failures else "SUCCEEDED",
        "chains": list(chains),
        "observations_inserted": inserted,
        "provider_failures": failures,
        "paper_trading_enabled": False,
        "live_execution_enabled": False,
    }, sort_keys=True))
    if failures and not any(inserted.values()):
        raise SystemExit("all configured chain market provider calls failed")


if __name__ == "__main__":
    main()
