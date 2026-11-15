"""Initialize and verify a separate production Turso schema without running providers.

This creates empty application/paper tables only. It does not migrate a local
SQLite ledger or activate paper/live execution.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from veltrix.config import settings
from veltrix.database import connect_database, is_remote_database, row_factory_for
from veltrix.foundation import validate_runtime_controls
from veltrix.ledger import PaperBroker
from veltrix.orchestration import OrchestrationStore
from veltrix.solana_meme.consensus_store import ConsensusStore
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore

CONFORMANCE_HOST = "veltrix-h0-conformance-test-nexora-apparel.aws-eu-central-1.turso.io"
EXPECTED_TABLES = {
    "solana_launch_candidates", "launch_feed_events", "launch_feed_outages",
    "solana_market_snapshots", "solana_risk_evidence_reports",
    "solana_consensus_decisions", "solana_enrichment_jobs",
    "solana_market_refresh_jobs", "solana_risk_evidence_jobs",
    "solana_consensus_jobs", "orchestration_jobs",
    "orchestration_worker_lease", "orchestration_audit",
    "account", "positions", "trades", "events", "position_state",
    "paper_consensus_decisions", "paper_execution_costs",
}


def main() -> None:
    validate_runtime_controls()
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        raise SystemExit("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are required")
    if not is_remote_database(url):
        raise SystemExit("production initialization requires an explicit Turso URL")
    parsed = urlsplit(url)
    if parsed.scheme != "libsql" or not (parsed.hostname or "").endswith(".turso.io"):
        raise SystemExit("TURSO_DATABASE_URL must be a credential-free libsql://*.turso.io URL")
    if parsed.hostname == CONFORMANCE_HOST:
        raise SystemExit("refusing to initialize the disposable conformance database")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise SystemExit("TURSO_DATABASE_URL must not contain credentials or URL extras")
    if os.getenv("LIVE_TRADING", "false").lower() not in {"false", "0", "no", "off", ""}:
        raise SystemExit("LIVE_TRADING must remain false")
    if os.getenv("PAPER_TRADING_ENABLED", "false").lower() not in {"false", "0", "no", "off", ""}:
        raise SystemExit("paper trading must remain disabled during schema initialization")

    config = settings()
    # Constructors create their own schema and indexes. No market/provider calls
    # are made here. PaperBroker inserts the initial account row only if absent.
    LaunchStore(url)
    MarketSnapshotStore(url)
    SolanaRiskEvidenceStore(url)
    ConsensusStore(url)
    PaperBroker(
        url,
        starting_balance=config.balance,
        max_positions=config.max_open,
        max_position_usd=config.max_position,
        stop_loss_pct=config.stop_loss,
    )
    OrchestrationStore(url).initialize()

    db = connect_database(url)
    db.row_factory = row_factory_for(url)
    try:
        actual = {
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        db.close()
    missing = sorted(EXPECTED_TABLES - actual)
    if missing:
        raise SystemExit("production schema verification failed: " + ", ".join(missing))

    print(json.dumps({
        "status": "SCHEMA_READY",
        "database_host": parsed.hostname,
        "tables_verified": len(EXPECTED_TABLES),
        "paper_ledger_initialized": True,
        "paper_trading_enabled": False,
        "live_execution_enabled": False,
        "data_migration_performed": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
