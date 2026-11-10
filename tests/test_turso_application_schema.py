"""Destructive application-schema smoke test for a dedicated disposable DB.

This test creates and drops application tables with production-like names.
It must only run against the explicitly designated H0 conformance database.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest


URL = os.getenv("TURSO_CONFORMANCE_DATABASE_URL", "").strip()
TOKEN = os.getenv("TURSO_CONFORMANCE_AUTH_TOKEN", "").strip()
EXPECTED_HOST = "veltrix-h0-conformance.turso.io"

pytestmark = pytest.mark.skipif(
    not URL or not TOKEN,
    reason="Dedicated Turso conformance credentials are required",
)


def _assert_disposable_target() -> None:
    parsed = urlsplit(URL)
    if parsed.scheme != "libsql" or parsed.hostname != EXPECTED_HOST:
        raise RuntimeError(
            "Refusing destructive schema test: database must be the exact "
            "dedicated veltrix-h0-conformance.turso.io endpoint"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("Refusing destructive schema test: URL contains credentials or extras")


def test_core_paper_pipeline_stores_initialize_on_turso(monkeypatch):
    _assert_disposable_target()
    monkeypatch.setenv("TURSO_AUTH_TOKEN", TOKEN)

    import turso_serverless

    from veltrix.ledger import PaperBroker
    from veltrix.solana_meme.consensus_store import ConsensusStore
    from veltrix.solana_meme.launch_stream import LaunchStore
    from veltrix.solana_meme.market_store import MarketSnapshotStore
    from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore

    stores = []
    try:
        stores.append(LaunchStore(URL))
        stores.append(MarketSnapshotStore(URL))
        stores.append(SolanaRiskEvidenceStore(URL))
        stores.append(ConsensusStore(URL))
        PaperBroker(URL, starting_balance=500.0)

        connection = turso_serverless.connect(URL, auth_token=TOKEN)
        try:
            expected = {
                "solana_launch_candidates", "launch_feed_events",
                "launch_feed_outages", "solana_market_snapshots",
                "solana_risk_evidence_reports", "solana_consensus_decisions",
                "account", "positions", "trades", "events", "position_state",
                "paper_consensus_decisions", "paper_execution_costs",
            }
            actual = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert expected <= actual
        finally:
            connection.close()
    finally:
        cleanup = turso_serverless.connect(URL, auth_token=TOKEN)
        try:
            for table in (
                "paper_execution_costs", "paper_consensus_decisions",
                "position_state", "events", "trades", "positions",
                "account", "solana_consensus_decisions",
                "solana_risk_evidence_reports", "solana_market_snapshots",
                "launch_feed_outages", "launch_feed_events",
                "solana_launch_candidates",
            ):
                cleanup.execute(f'DROP TABLE IF EXISTS "{table}"')
            cleanup.commit()
        finally:
            cleanup.close()
