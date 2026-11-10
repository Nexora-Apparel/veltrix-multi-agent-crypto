"""Live read-only dashboard conformance against the disposable Turso DB."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

DATABASE_URL = os.getenv("TURSO_CONFORMANCE_DATABASE_URL", "").strip()
AUTH_TOKEN = os.getenv("TURSO_CONFORMANCE_AUTH_TOKEN", "").strip()
EXPECTED_HOST = "veltrix-h0-conformance-test-nexora-apparel.aws-eu-central-1.turso.io"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not AUTH_TOKEN,
    reason="dedicated disposable Turso conformance credentials are required",
)


def test_dashboard_reads_turso_wallet_and_enforces_query_only():
    parsed = urlsplit(DATABASE_URL)
    assert parsed.scheme == "libsql" and parsed.hostname == EXPECTED_HOST
    assert parsed.username is None and parsed.password is None
    os.environ["TURSO_AUTH_TOKEN"] = AUTH_TOKEN

    from veltrix.ledger import PaperBroker
    from veltrix.dashboard_server import DashboardRepository
    from veltrix.solana_meme.sol_paper_wallet import SolanaPaperWallet, LAMPORTS_PER_SOL

    PaperBroker(DATABASE_URL, starting_balance=500, max_positions=10, max_position_usd=50)
    SolanaPaperWallet(DATABASE_URL, LAMPORTS_PER_SOL)
    repository = DashboardRepository(DATABASE_URL)

    health = repository.health()
    assert health["database"] == "connected"
    assert health["database_read_only"] is True
    assert health["live_execution"] is False
    wallet = repository.sol_paper_wallet()
    assert wallet["configured"] is True
    assert wallet["unit"] == "SOL"
    assert wallet["cash_lamports"] == LAMPORTS_PER_SOL

    with repository._connect() as db:
        with pytest.raises(Exception):
            db.execute(
                "CREATE TABLE veltrix_dashboard_write_must_fail(id INTEGER)"
            )
