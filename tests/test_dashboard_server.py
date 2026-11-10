import json
import sqlite3
import threading
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
