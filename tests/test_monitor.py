from veltrix.monitor import MarketMonitor
from veltrix.providers.base import MarketEvent
from veltrix.discovery import TokenCandidate

class FakeProvider:
    def fetch_token_pairs(self, chain, addresses):
        return [MarketEvent(chain, addresses[0], "pair", "TST", 1.0, 5000, 2000, 1.5, 1000, None, "fake")]

class FakeDiscovery:
    def discover(self, chains):
        return [TokenCandidate("base", "discovered-token", "fake-feed", 1000)]

def test_monitor_persists_market_observations(tmp_path):
    db = str(tmp_path / "monitor.db")
    monitor = MarketMonitor({"base": ["token-address"]}, db, FakeProvider())
    events = monitor.poll_once()
    assert len(events) == 1
    import sqlite3
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM events WHERE event='MARKET_OBSERVATION'").fetchone()[0]
    assert count == 1

def test_monitor_discovers_and_fetches_candidate(tmp_path):
    db = str(tmp_path / "discovery.db")
    monitor = MarketMonitor({}, db, FakeProvider(), FakeDiscovery(), True, ("base",), 5)
    events = monitor.poll_once()
    assert len(events) == 1 and events[0].token_address == "discovered-token"
    import sqlite3
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE event='DISCOVERY_CANDIDATE'").fetchone()[0] == 1
