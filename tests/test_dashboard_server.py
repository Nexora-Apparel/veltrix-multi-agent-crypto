import json
import sqlite3
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from veltrix.ledger import PaperBroker
from veltrix.dashboard_server import DashboardRepository, build_server
from veltrix.solana_meme.consensus_store import ConsensusStore
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore


def make_database(tmp_path):
    path = tmp_path / "dashboard-server.sqlite3"
    PaperBroker(str(path), starting_balance=500)
    MarketSnapshotStore(path)
    ConsensusStore(path)
    SolanaRiskEvidenceStore(path)
    return path


def test_dashboard_server_api_is_authenticated_and_get_only(tmp_path):
    database = make_database(tmp_path)
    token = "v" * 40
    server = build_server(
        database_path=database,
        host="127.0.0.1",
        port=0,
        api_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(HTTPError) as denied:
            urlopen(base + "/api/overview")
        assert denied.value.code == 401

        request = Request(
            base + "/api/health",
            headers={"Authorization": "Bearer " + token},
        )
        health = json.loads(urlopen(request).read())
        assert health["database"] == "connected"
        assert health["database_read_only"] is True
        assert health["live_execution"] is False

        request = Request(
            base + "/api/overview",
            headers={"Authorization": "Bearer " + token},
        )
        overview = json.loads(urlopen(request).read())
        assert overview["read_only"] is True
        assert overview["live_execution"] is False

        request = Request(
            base + "/",
            headers={"Authorization": "Bearer " + token},
        )
        page = urlopen(request).read().decode("utf-8")
        assert "VELTRIX" in page
        assert "SIMULATION ONLY" in page

        request = Request(
            base + "/assets/dashboard.js",
            headers={"Authorization": "Bearer " + token},
        )
        assert urlopen(request).status == 200

        write = Request(
            base + "/api/overview",
            data=b"{}",
            method="POST",
            headers={"Authorization": "Bearer " + token},
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(write)
        assert rejected.value.code == 405
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_dashboard_repository_opens_database_read_only(tmp_path):
    database = make_database(tmp_path)
    repository = DashboardRepository(database)

    with repository._connect() as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE must_not_exist(id INTEGER)")


def test_dashboard_remote_binding_requires_at_least_32_token_characters(tmp_path):
    database = make_database(tmp_path)
    with pytest.raises(ValueError, match="at least 32 characters"):
        build_server(
            database_path=database,
            host="0.0.0.0",
            port=0,
            api_token="short",
        )
    with pytest.raises(ValueError, match="at least 32 characters"):
        build_server(
            database_path=database,
            host="0.0.0.0",
            port=0,
            api_token="",
        )

    server = build_server(
        database_path=database,
        host="0.0.0.0",
        port=0,
        api_token="x" * 32,
    )
    server.server_close()


def test_dashboard_api_token_environment_variable_is_used(tmp_path, monkeypatch):
    database = make_database(tmp_path)
    monkeypatch.setenv("VELTRIX_DASHBOARD_TOKEN", "e" * 32)
    server = build_server(
        database_path=database,
        host="0.0.0.0",
        port=0,
    )
    server.server_close()



def add_dashboard_market(store, *, quality_flags=(), price=1.5, observed_at_ms=None):
    import time

    observed = observed_at_ms or int(time.time() * 1000)
    store.record(SolanaMarketSnapshot(
        mint="So11111111111111111111111111111111111111112",
        selected_pair_address="dashboard-quality-pool",
        symbol="TEST",
        name="Dashboard Quality Test",
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
        retrieved_at_ms=observed,
        response_received_at_ms=observed,
        pair_created_at_ms=observed - 3_600_000,
        quality_flags=tuple(quality_flags),
    ))


def test_dashboard_never_marks_quality_flagged_market_as_fresh(tmp_path):
    database = make_database(tmp_path)
    broker = PaperBroker(str(database), starting_balance=500)
    broker.open_position(
        "So11111111111111111111111111111111111111112",
        1.0,
        20,
        "dashboard quality test",
    )
    markets = MarketSnapshotStore(database)
    add_dashboard_market(
        markets,
        quality_flags=("liquidity_usd_missing",),
        price=2.0,
    )

    repository = DashboardRepository(database)
    positions = repository.positions()
    overview = repository.overview()

    assert len(positions) == 1
    assert positions[0]["mark_status"] == "QUALITY_FLAGGED"
    assert positions[0]["mark_price"] == pytest.approx(1.0)
    assert positions[0]["quality_flags"] == ["liquidity_usd_missing"]
    assert overview["account"]["stale_or_unmarked_positions"] == 1
    assert overview["account"]["quality_flagged_positions"] == 1
    assert overview["account"]["equity_usd"] == pytest.approx(500.0)


def test_dashboard_malformed_quality_flags_fail_closed(tmp_path):
    database = make_database(tmp_path)
    broker = PaperBroker(str(database), starting_balance=500)
    broker.open_position(
        "So11111111111111111111111111111111111111112",
        1.0,
        20,
        "dashboard malformed quality test",
    )
    markets = MarketSnapshotStore(database)
    add_dashboard_market(markets, price=2.0)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE solana_market_snapshots SET quality_flags_json='null' WHERE mint=?",
            ("So11111111111111111111111111111111111111112",),
        )

    position = DashboardRepository(database).positions()[0]
    assert position["mark_status"] == "QUALITY_FLAGGED"
    assert position["mark_price"] == pytest.approx(1.0)
    assert position["quality_flags"] == ["malformed_quality_flags"]


def test_dashboard_api_all_routes_are_authenticated_get_only(tmp_path):
    database = make_database(tmp_path)
    token = "z" * 40
    server = build_server(
        database_path=database,
        host="127.0.0.1",
        port=0,
        api_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for path in (
            "/api/health",
            "/api/overview",
            "/api/positions",
            "/api/trades",
            "/api/decisions",
            "/api/consensus",
            "/api/markets",
            "/api/equity-curve",
            "/api/costs",
        ):
            request = Request(
                base + path,
                headers={"Authorization": "Bearer " + token},
            )
            response = urlopen(request)
            assert response.status == 200
            assert response.headers.get_content_type() == "application/json"
            json.loads(response.read())

        for method in ("POST", "PUT", "DELETE"):
            request = Request(
                base + "/api/overview",
                data=b"{}",
                method=method,
                headers={"Authorization": "Bearer " + token},
            )
            with pytest.raises(HTTPError) as rejected:
                urlopen(request)
            assert rejected.value.code == 405
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_dashboard_defaults_to_loopback_binding(tmp_path, monkeypatch):
    database = make_database(tmp_path)
    monkeypatch.delenv("VELTRIX_DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("VELTRIX_DASHBOARD_PORT", raising=False)
    monkeypatch.delenv("VELTRIX_DASHBOARD_TOKEN", raising=False)

    server = build_server(database_path=database, port=0)

    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
