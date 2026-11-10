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
    os.environ["TURSO_DASHBOARD_AUTH_TOKEN"] = AUTH_TOKEN

    from veltrix.ledger import PaperBroker
    from veltrix.dashboard_server import DashboardRepository
    from veltrix.solana_meme.sol_paper_wallet import SolanaPaperWallet, LAMPORTS_PER_SOL

    PaperBroker(DATABASE_URL, starting_balance=500, max_positions=10, max_position_usd=50)
    wallet_store = SolanaPaperWallet(DATABASE_URL, LAMPORTS_PER_SOL)
    repository = DashboardRepository(DATABASE_URL)

    health = repository.health()
    assert health["database"] == "connected"
    assert health["database_read_only"] is True
    assert health["live_execution"] is False
    wallet_state = repository.sol_paper_wallet()
    assert wallet_state["configured"] is True
    assert wallet_state["unit"] == "SOL"
    assert wallet_state["cash_lamports"] == LAMPORTS_PER_SOL

    import time
    mint = "So11111111111111111111111111111111111111112"
    now_ms = time.time_ns() // 1_000_000
    provenance = {
        "mint": mint, "market_snapshot_id": 1,
        "token_pair_address": "turso-test-token-pool",
        "sol_usd_pair_address": "turso-test-sol-usdc-pool",
        "sol_usd_method": "wsol_stable_pool",
        "token_price_usd": "0.001", "sol_price_usd": "100",
        "token_observed_at_ms": now_ms, "sol_observed_at_ms": now_ms,
        "token_source": "turso-conformance", "sol_source": "turso-conformance",
    }
    # Isolate the fixed probe mint so repeated conformance runs are idempotent.
    with wallet_store._transaction() as db:
        db.execute("DELETE FROM solana_paper_marks WHERE mint=?", (mint,))
        db.execute("DELETE FROM solana_paper_positions WHERE mint=?", (mint,))
        db.execute("DELETE FROM solana_paper_position_state WHERE mint=?", (mint,))
        db.execute("DELETE FROM solana_paper_fills WHERE mint=?", (mint,))
    wallet_store.open_position(
        idempotency_key=f"turso-mark-entry-{now_ms}", mint=mint, quantity=1,
        price_lamports=1_000, fee_lamports=0, created_at_ms=now_ms,
        quote_provenance=provenance,
    )
    wallet_store.process_market_price(
        mint=mint, price_lamports=1_500, market_snapshot_id=1,
        created_at_ms=now_ms + 1, quote_provenance=provenance,
    )
    marked = repository.sol_paper_wallet()
    assert marked["equity_complete"] is True
    assert marked["unrealized_pnl_lamports"] == 500
    assert marked["open_positions"][0]["mark_source"] == "turso-conformance"
    wallet_store.close_position(
        idempotency_key=f"turso-mark-exit-{now_ms}", mint=mint,
        price_lamports=1_000, fee_lamports=0, created_at_ms=now_ms + 2,
        quote_provenance=provenance,
    )
    with wallet_store._transaction() as db:
        db.execute("DELETE FROM solana_paper_marks WHERE mint=?", (mint,))
        db.execute("DELETE FROM solana_paper_positions WHERE mint=?", (mint,))
        db.execute("DELETE FROM solana_paper_position_state WHERE mint=?", (mint,))
        db.execute("DELETE FROM solana_paper_fills WHERE mint=?", (mint,))

    with repository._connect() as db:
        with pytest.raises(Exception):
            db.execute(
                "CREATE TABLE veltrix_dashboard_write_must_fail(id INTEGER)"
            )
