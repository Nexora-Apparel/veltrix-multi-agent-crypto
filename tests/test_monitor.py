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

def test_monitor_builds_scout_context_only_after_minimum_history(tmp_path):
    from veltrix.providers.base import MarketEvent
    db = str(tmp_path / "signals.db")
    monitor = MarketMonitor({"base": ["token-address"]}, db, FakeProvider())
    import time
    now_ms = int(time.time() * 1000)
    for i in range(5):
        monitor.ledger.record_market_event(MarketEvent(
            "base", "token-address", f"old-{i}", "TST", 1.0, 5000,
            1000, 0.5, now_ms - (5 - i) * 60_000, None, "fake"
        ))
    monitor.provider = type("SpikeProvider", (), {
        "fetch_token_pairs": lambda self, chain, addresses: [
            MarketEvent(chain, addresses[0], "new", "TST", 1.1, 6000,
                        3000, 2.0, int(time.time() * 1000), None, "fake")
        ]
    })()
    monitor.poll_once()
    assert len(monitor.signal_contexts) == 1
    context = monitor.signal_contexts[0]
    assert context["volume_spike"] is True
    assert context["volume_multiple"] == 3.0
    assert context["signal_quality"] == "heuristic"


def test_monitor_does_not_emit_volume_signal_without_history(tmp_path):
    monitor = MarketMonitor({"base": ["token-address"]}, str(tmp_path / "empty.db"), FakeProvider())
    monitor.poll_once()
    assert monitor.signal_contexts == []
