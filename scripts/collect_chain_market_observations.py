"""Manual one-shot production market observation collector for all supported chains."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from veltrix.chain_market_ingestion import CHAINS, ChainMarketObservationCollector
from veltrix.database import is_remote_database
from veltrix.foundation import validate_runtime_controls


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
    discover = os.getenv("VELTRIX_DISCOVER_LATEST_PROFILES", "false").lower() in {"1", "true", "yes", "on"}
    limit = int(os.getenv("VELTRIX_CHAIN_MARKET_LIMIT_PER_CHAIN", "25"))
    collector = ChainMarketObservationCollector(url)
    result = collector.run_once(
        tokens, discover_latest_profiles=discover, limit_per_chain=limit,
    )
    print(json.dumps(result, sort_keys=True))
    if result["provider_failures"] and not any(result["observations_inserted"].values()):
        raise SystemExit("all configured chain market provider calls failed")


if __name__ == "__main__":
    main()
