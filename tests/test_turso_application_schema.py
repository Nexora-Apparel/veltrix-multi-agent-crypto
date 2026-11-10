"""Remote schema smoke test for the existing core persistence stores.

This writes only to the disposable H0 Turso database. Never point these
credentials at the production paper ledger.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest


URL = os.getenv("TURSO_CONFORMANCE_DATABASE_URL", "").strip()
TOKEN = os.getenv("TURSO_CONFORMANCE_AUTH_TOKEN", "").strip()
EXPECTED_HOST = "veltrix-h0-conformance.turso.io"


def _assert_disposable_target(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "libsql"
        or parsed.hostname != EXPECTED_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise RuntimeError(f"schema cleanup is restricted to {EXPECTED_HOST}")

pytestmark = pytest.mark.skipif(
    not URL or not TOKEN,
    reason="dedicated disposable Turso conformance credentials are required",
)


def test_core_paper_pipeline_stores_initialize_on_turso(monkeypatch):
    _assert_disposable_target(URL)
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
                "solana_launch_candidates",
                "launch_feed_events",
                "launch_feed_outages",
                "solana_market_snapshots",
                "solana_risk_evidence_reports",
                "solana_consensus_decisions",
                "account",
                "positions",
                "trades",
                "events",
                "position_state",
                "paper_consensus_decisions",
                "paper_execution_costs",
            }
            # The direct Turso DB-API connection returns tuple rows by
            # default (unlike the application adapter, which configures
            # named-row access). This query selects one column, so index 0
            # is the portable DB-API representation.
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
                "paper_execution_costs",
                "paper_consensus_decisions",
                "position_state",
                "events",
                "trades",
                "positions",
                "account",
                "solana_consensus_decisions",
                "solana_risk_evidence_reports",
                "solana_market_snapshots",
                "launch_feed_outages",
                "launch_feed_events",
                "solana_launch_candidates",
            ):
                cleanup.execute(f'DROP TABLE IF EXISTS "{table}"')
            cleanup.commit()
        finally:
            cleanup.close()
