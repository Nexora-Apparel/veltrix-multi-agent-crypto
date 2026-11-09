import json
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from veltrix.ledger import PaperBroker
from veltrix.solana_meme.consensus_store import ConsensusStore
from veltrix.solana_meme.dashboard import (
    PaperDashboardHTTPServer,
    PaperDashboardReader,
    build_server,
)
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore

MINT = "So11111111111111111111111111111111111111112"


def add_market_snapshot(store, observed_ms, price=1.5):
    store.record(SolanaMarketSnapshot(
        mint=MINT,
        selected_pair_address="dashboard-pool",
        symbol="TEST",
        name="Dashboard Test",
        price_usd=price,
        selected_pool_liquidity_usd=100_000,
        selected_pool_volume_m5_usd=1_000,
        selected_pool_volume_h1_usd=50_000,
        selected_pool_volume_h24_usd=200_000,
        price_change_m5_pct=2.0,
        price_change_h1_pct=5.0,
        price_change_h24_pct=10.0,
        txns_m5_buys=20,
        txns_m5_sells=10,
        txns_h1_buys=200,
        txns_h1_sells=100,
        txns_h24_buys=1_000,
        txns_h24_sells=500,
        fdv_usd=1_000_000,
        market_cap_usd=900_000,
        dex_id="raydium",
        quote_token_symbol="SOL",
        pool_token_side="base",
        pair_count=1,
        retrieved_at_ms=observed_ms,
        response_received_at_ms=observed_ms,
        pair_created_at_ms=observed_ms - 3_600_000,
        quality_flags=(),
    ))


def build_dashboard_db(tmp_path, *, stale=False):
    db_path = tmp_path / "dashboard.sqlite3"
    broker = PaperBroker(str(db_path), starting_balance=500, max_positions=10, max_position_usd=50)
    markets = MarketSnapshotStore(db_path)
    ConsensusStore(db_path)
    SolanaRiskEvidenceStore(db_path)
    now = int(time.time() * 1000)
    add_market_snapshot(markets, now - 500_000 if stale else now)
    broker.open_position(MINT, 1.0, 20, "dashboard test entry")
    broker.record_consensus_outcome(
        "decision-dashboard", MINT, "ABSTAIN", "NOT_ELIGIBLE",
        "insufficient consensus support", created_at_ms=now, processed_at_ms=now,
    )
    return db_path, broker, markets, now


def test_dashboard_overview_marks_positions_and_reports_realized_performance(tmp_path):
    db_path, broker, markets, now = build_dashboard_db(tmp_path)
    reader = PaperDashboardReader(db_path, clock_ms=lambda: now)

    overview = reader.overview()

    assert overview["account"]["cash_usd"] == pytest.approx(480)
    assert overview["account"]["equity_usd"] == pytest.approx(510)
    assert overview["account"]["unrealized_pnl_usd"] == pytest.approx(10)
    assert overview["account"]["portfolio_exposure_usd"] == pytest.approx(20)
    assert overview["freshness"]["stale_open_positions"] == 0

    close = broker.close_fraction(MINT, 1.5, 1.0, "test close", fee_bps=100, slippage_bps=50)
    assert close["realized_pnl"] > 0

    after = reader.overview()
    assert after["account"]["realized_pnl_usd"] == pytest.approx(close["realized_pnl"])
    assert after["performance"]["closed_fills"] == 1
    assert after["costs"]["fees_usd"] == pytest.approx(close["fee_usd"])
    assert after["costs"]["slippage_cost_usd"] == pytest.approx(close["slippage_cost_usd"])
    assert reader.decisions(limit=5)[0]["execution_status"] == "NOT_ELIGIBLE"
    assert reader.performance(days=30)["daily"]


def test_dashboard_flags_stale_market_marks_without_hiding_positions(tmp_path):
    db_path, _, _, now = build_dashboard_db(tmp_path, stale=True)
    reader = PaperDashboardReader(
        db_path, clock_ms=lambda: now, max_market_age_ms=60_000
    )

    positions = reader.positions()
    overview = reader.overview()

    assert len(positions) == 1
    assert positions[0]["mark_status"] == "stale"
    assert overview["freshness"]["stale_open_positions"] == 1


def test_dashboard_database_connection_is_read_only(tmp_path):
    db_path, _, _, _ = build_dashboard_db(tmp_path)
    reader = PaperDashboardReader(db_path)
    connection = reader._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE dashboard_must_not_write(id INTEGER)")
    finally:
        connection.close()


def test_dashboard_api_requires_token_and_exposes_get_only_endpoints(tmp_path):
    db_path, _, _, _ = build_dashboard_db(tmp_path)
    token = "d" * 40
    reader = PaperDashboardReader(db_path)
    server = PaperDashboardHTTPServer(("127.0.0.1", 0), reader, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(HTTPError) as unauthorized:
            urlopen(base + "/api/overview")
        assert unauthorized.value.code == 401

        request = Request(base + "/api/overview", headers={"Authorization": "Bearer " + token})
        payload = json.loads(urlopen(request).read())
        assert payload["data"]["account"]["open_positions"] == 1

        health = json.loads(urlopen(base + "/healthz").read())
        assert health["mode"] == "read-only"

        write_request = Request(
            base + "/api/overview",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer " + token},
        )
        with pytest.raises(HTTPError) as forbidden:
            urlopen(write_request)
        assert forbidden.value.code == 405
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_remote_dashboard_binding_requires_long_access_token(tmp_path):
    db_path, _, _, _ = build_dashboard_db(tmp_path)
    with pytest.raises(ValueError, match="at least 32 characters"):
        build_server(
            host="0.0.0.0", port=8765, database_path=db_path, access_token="short"
        )

    server = build_server(
        host="0.0.0.0",
        port=8765,
        database_path=db_path,
        access_token="r" * 40,
    )
    server.server_close()
