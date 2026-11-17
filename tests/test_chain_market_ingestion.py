import sqlite3
from types import SimpleNamespace

import pytest

from veltrix.chain_market_ingestion import ChainMarketObservationCollector
from veltrix.providers.base import MarketEvent


class FakeMarketProvider:
    name = "fake-dexscreener"

    def __init__(self, events=None, error=None):
        self.events = events or []
        self.error = error

    def fetch_token_pairs(self, chain, addresses):
        if self.error:
            raise self.error
        return self.events


class FakeDiscovery:
    def discover(self, chains):
        return [SimpleNamespace(chain="base", token_address="0xDiscovered")]


def market_event(chain="base", token="0xToken", price=1.5):
    return MarketEvent(
        chain=chain, token_address=token, pair_address="0xPool", symbol="TST",
        price_usd=price, liquidity_usd=5000, volume_h1_usd=2000,
        price_change_m5_pct=1.0, observed_at_ms=1_700_000_000_000,
        pair_created_at_ms=None, source="dexscreener",
    )


def test_chain_market_collector_persists_only_market_events(tmp_path):
    path = str(tmp_path / "market.db")
    collector = ChainMarketObservationCollector(
        path, provider=FakeMarketProvider([market_event()]),
        clock_ms=lambda: 1_700_000_000_000,
    )
    result = collector.run_once({"base": ["0xToken"]})
    assert result["status"] == "SUCCEEDED"
    assert result["observations_inserted"]["base"] == 1
    assert result["paper_trading_enabled"] is False
    assert result["live_execution_enabled"] is False
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT event,payload FROM events ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["PROVIDER_PROBE", "MARKET_OBSERVATION"]
    assert '"chain":"base"' in rows[1][1]


def test_chain_market_collector_discovers_and_bounds_chain_candidates(tmp_path):
    provider = FakeMarketProvider([market_event(token="0xDiscovered")])
    collector = ChainMarketObservationCollector(
        str(tmp_path / "discover.db"), provider=provider, discovery=FakeDiscovery(),
        clock_ms=lambda: 1_700_000_000_000,
    )
    result = collector.run_once({}, discover_latest_profiles=True, limit_per_chain=1)
    assert result["chains"] == ["base"]
    assert result["observations_inserted"]["base"] == 1


def test_chain_market_collector_records_provider_failure_without_market_rows(tmp_path):
    path = str(tmp_path / "failure.db")
    collector = ChainMarketObservationCollector(
        path, provider=FakeMarketProvider(error=TimeoutError("private endpoint details")),
        clock_ms=lambda: 1_700_000_000_000,
    )
    result = collector.run_once({"bsc": ["0xToken"]})
    assert result["status"] == "PARTIAL"
    assert result["provider_failures"] == {"bsc": "TimeoutError"}
    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT event,payload FROM events").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "PROVIDER_PROBE"
    assert "private endpoint details" not in rows[0][1]


def test_chain_market_collector_rejects_empty_input_without_discovery(tmp_path):
    collector = ChainMarketObservationCollector(str(tmp_path / "empty.db"))
    with pytest.raises(ValueError, match="at least one chain token"):
        collector.run_once({})



def test_remote_write_connection_opens_only_after_provider_io(tmp_path, monkeypatch):
    import veltrix.chain_market_ingestion as ingestion

    path = str(tmp_path / "connection-order.db")
    connection_opened = {"value": False}

    class OrderedProvider(FakeMarketProvider):
        def fetch_token_pairs(self, chain, addresses):
            assert connection_opened["value"] is False
            return [market_event(chain=chain, token=addresses[0])]

    real_connect = ingestion.connect_database

    def tracked_connect(database_path):
        connection_opened["value"] = True
        return real_connect(database_path)

    monkeypatch.setattr(ingestion, "connect_database", tracked_connect)
    collector = ChainMarketObservationCollector(
        path,
        provider=OrderedProvider(),
        clock_ms=lambda: 1_700_000_000_000,
    )
    result = collector.run_once({"base": ["0xToken"]})

    assert connection_opened["value"] is True
    assert result["observations_inserted"]["base"] == 1
