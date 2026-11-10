import json
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from veltrix.ledger import PaperBroker
from veltrix.dashboard_server import DashboardRepository, build_server
from veltrix.solana_meme.consensus_store import ConsensusStore
from veltrix.solana_meme.candle_data import SolanaCandle
from veltrix.solana_meme.candle_store import SolanaCandleStore
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
        assert health["worker"] == {"status": "not_configured", "healthy": False}
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



def test_dashboard_loopback_binding_also_requires_strong_token(tmp_path):
    database = make_database(tmp_path)
    for token in ("", "short", "x" * 31):
        with pytest.raises(ValueError, match="at least 32 characters"):
            build_server(
                database_path=database,
                host="127.0.0.1",
                port=0,
                api_token=token,
            )


def test_dashboard_missing_environment_token_fails_closed(tmp_path, monkeypatch):
    database = make_database(tmp_path)
    monkeypatch.delenv("VELTRIX_DASHBOARD_TOKEN", raising=False)
    with pytest.raises(ValueError, match="at least 32 characters"):
        build_server(database_path=database, host="127.0.0.1", port=0)

def test_dashboard_api_token_environment_variable_is_used(tmp_path, monkeypatch):
    database = make_database(tmp_path)
    monkeypatch.setenv("VELTRIX_DASHBOARD_TOKEN", "e" * 32)
    server = build_server(
        database_path=database,
        host="0.0.0.0",
        port=0,
    )
    server.server_close()


def test_dashboard_candle_endpoint_contract_returns_real_stored_candles(tmp_path):
    database = make_database(tmp_path)
    store = SolanaCandleStore(str(database))
    now = 1_800_000_000_000
    store.record(SolanaCandle(
        mint="So11111111111111111111111111111111111111112",
        pair_address="pool-chart", interval="1m", open_time_ms=now,
        open=0.001, high=0.0012, low=0.0009, close=0.0011,
        volume=1200, source="fixture-provider",
        observed_at_ms=now + 59000, is_closed=True,
    ))
    repository = DashboardRepository(database)
    result = repository.candles(
        mint="So11111111111111111111111111111111111111112",
        pair_address="pool-chart", interval="1m",
    )
    assert result["count"] == 1
    assert result["candles"][0]["close"] == 0.0011
    assert result["source"] == "fixture-provider"
    assert result["synthetic"] is False


def test_dashboard_candle_endpoint_rejects_invalid_interval_and_missing_pair(tmp_path):
    repository = DashboardRepository(make_database(tmp_path))
    with pytest.raises(ValueError, match="unsupported"):
        repository.candles(
            mint="So11111111111111111111111111111111111111112",
            pair_address="pool", interval="2m",
        )
    with pytest.raises(ValueError, match="pair_address"):
        repository.candles(
            mint="So11111111111111111111111111111111111111112",
            pair_address="", interval="1m",
        )


def test_dashboard_pool_resolution_rejects_invalid_mint_without_provider_access(tmp_path):
    database = make_database(tmp_path)
    token = "p" * 40
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
        request = Request(
            base + "/api/pool?mint=not-a-solana-mint",
            headers={"Authorization": "Bearer " + token},
        )
        with pytest.raises(HTTPError) as rejected:
            urlopen(request)
        assert rejected.value.code == 400
        payload = json.loads(rejected.value.read())
        assert "mint" in payload["error"].lower()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_dashboard_sol_wallet_endpoint_reports_native_lamports_and_pnl(tmp_path):
    from veltrix.solana_meme.sol_paper_wallet import SolanaPaperWallet, LAMPORTS_PER_SOL

    database = make_database(tmp_path)
    wallet = SolanaPaperWallet(str(database), 2 * LAMPORTS_PER_SOL)
    before = DashboardRepository(database).sol_paper_wallet()
    assert before["configured"] is True
    assert before["cash_lamports"] == 2 * LAMPORTS_PER_SOL
    assert before["equity_complete"] is True

    mint = "So11111111111111111111111111111111111111112"
    wallet.open_position(
        idempotency_key="dashboard-entry", mint=mint, quantity=10,
        price_lamports=1_000_000, fee_lamports=100_000,
        created_at_ms=1_800_000_000_000,
    )
    open_state = DashboardRepository(database).sol_paper_wallet()
    assert open_state["open_position_count"] == 1
    assert open_state["equity_lamports"] is None
    assert open_state["equity_complete"] is False

    wallet.close_position(
        idempotency_key="dashboard-exit", mint=mint, price_lamports=2_000_000,
        fee_lamports=100_000, created_at_ms=1_800_000_001_000,
    )
    closed = DashboardRepository(database).sol_paper_wallet()
    assert closed["open_position_count"] == 0
    assert closed["realized_pnl_lamports"] == 9_800_000
    assert closed["equity_lamports"] == 2 * LAMPORTS_PER_SOL + 9_800_000
    assert closed["live_execution"] is False


def test_dashboard_remote_database_adapter_remains_query_only(monkeypatch, tmp_path):
    import sqlite3
    import veltrix.dashboard_server as dashboard_module

    database = make_database(tmp_path)
    monkeypatch.setattr(
        dashboard_module, "connect_database",
        lambda _path, timeout=3, auth_token=None: sqlite3.connect(database, timeout=timeout),
    )
    monkeypatch.setattr(dashboard_module, "row_factory_for", lambda _path: sqlite3.Row)
    repository = DashboardRepository("libsql://example.aws-eu-central-1.turso.io")
    assert repository.health()["database"] == "connected"
    with repository._connect() as db:
        for statement in (
            "CREATE TABLE dashboard_write_must_fail(id INTEGER)",
            "DELETE FROM account",
            "PRAGMA query_only=OFF",
            "SELECT 1; DELETE FROM account",
            "WITH x AS (SELECT 1) DELETE FROM account",
        ):
            with pytest.raises(sqlite3.OperationalError, match="read-only|multiple SQL"):
                db.execute(statement)
        assert db.execute("SELECT 1").fetchone()[0] == 1


def test_dashboard_health_marks_old_pipeline_heartbeat_stale(tmp_path, monkeypatch):
    database = make_database(tmp_path)
    now_ms = int(time.time() * 1000)
    with sqlite3.connect(database) as db:
        db.execute("""
            CREATE TABLE solana_pipeline_health (
                id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                last_started_at_ms INTEGER, last_finished_at_ms INTEGER,
                last_success_at_ms INTEGER, last_duration_ms INTEGER,
                last_job_id TEXT, last_error TEXT,
                consecutive_failures INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
            )
        """)
        db.execute("""
            INSERT INTO solana_pipeline_health
            VALUES (1,'healthy',?,?,?,?,?,?,0,?)
        """, (now_ms - 60_000, now_ms - 59_000, now_ms - 59_000,
              1000, "job-1", None, now_ms - 60_000))
    monkeypatch.setenv("VELTRIX_PIPELINE_STALE_AFTER_SECONDS", "30")
    health = DashboardRepository(database).health()
    assert health["worker"]["reported_status"] == "healthy"
    assert health["worker"]["status"] == "stale"
    assert health["worker"]["stale"] is True
    assert health["worker"]["healthy"] is False
    assert health["worker"]["health_age_seconds"] >= 30


def test_dashboard_health_exposes_candle_refresh_queue_and_retry_state(tmp_path):
    database = make_database(tmp_path)
    now_ms = int(time.time() * 1000)
    with sqlite3.connect(database) as db:
        db.execute("""
            CREATE TABLE solana_candle_refresh_jobs (
                mint TEXT NOT NULL, interval TEXT NOT NULL, pair_address TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                next_refresh_at_ms INTEGER NOT NULL, lease_until_ms INTEGER,
                claimed_by_owner_id TEXT, claim_fencing_token INTEGER,
                last_success_at_ms INTEGER, last_error TEXT, updated_at_ms INTEGER NOT NULL,
                PRIMARY KEY(mint, interval)
            )
        """)
        db.executemany("""
            INSERT INTO solana_candle_refresh_jobs
            (mint,interval,pair_address,status,consecutive_failures,
             next_refresh_at_ms,last_success_at_ms,updated_at_ms)
            VALUES (?,?,?,?,?,?,?,?)
        """, [
            ("mint-a", "1m", "pool-a", "active", 0, now_ms, now_ms - 10_000, now_ms),
            ("mint-b", "1m", "pool-b", "retry", 2, now_ms, now_ms - 20_000, now_ms),
        ])
    health = DashboardRepository(database).health()
    assert health["candle_refresh"]["status"] == "degraded"
    assert health["candle_refresh"]["total_jobs"] == 2
    assert health["candle_refresh"]["retry_jobs"] == 1
    assert health["candle_refresh"]["processing_jobs"] == 0
    assert health["candle_refresh"]["consecutive_failures"] == 2
    assert health["candle_refresh"]["last_success_age_seconds"] >= 0
