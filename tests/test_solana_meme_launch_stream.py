import json
import sqlite3
import threading

import pytest

from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.launch_stream import LaunchStore, LaunchStreamManager

MINT = "So11111111111111111111111111111111111111112"


def candidate(source="pumpportal-new-token", timestamp=1000):
    return normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source=source,
        observed_at_ms=timestamp,
    )


def test_store_persists_candidates_and_merges_sources_without_losing_first_seen(tmp_path):
    store = LaunchStore(tmp_path / "launches.sqlite3")
    store.record_candidate(candidate("pumpportal-new-token", 1200))
    store.record_candidate(candidate("dexscreener-latest-profile", 1400))
    store.record_candidate(candidate("older-source", 1100))

    rows = store.list_candidates()
    assert store.candidate_count() == 1
    assert rows[0]["first_seen_at_ms"] == 1100
    assert rows[0]["latest_seen_at_ms"] == 1400
    assert rows[0]["primary_source"] == "dexscreener-latest-profile"
    assert rows[0]["sources"] == [
        "dexscreener-latest-profile",
        "older-source",
        "pumpportal-new-token",
    ]


def test_store_supports_shared_in_memory_database():
    store = LaunchStore(":memory:")
    store.record_candidate(candidate())
    assert store.candidate_count() == 1
    assert store.list_candidates()[0]["mint"] == MINT


def test_outages_are_tracked_and_recovered_per_source(tmp_path):
    store = LaunchStore(tmp_path / "coverage.sqlite3")
    store.record_outage("profile endpoint down", source="dexscreener-latest-profile",
                        occurred_at_ms=2000)
    store.record_outage("boost endpoint down", source="dexscreener-latest-boost",
                        occurred_at_ms=2100)
    store.record_outage("duplicate profile outage", source="dexscreener-latest-profile",
                        occurred_at_ms=2200)

    assert store.active_outage_count() == 2
    assert store.active_outage_count(source="dexscreener-latest-profile") == 1

    store.record_source_healthy("dexscreener-latest-profile", occurred_at_ms=3000)
    assert store.active_outage_count(source="dexscreener-latest-profile") == 0
    assert store.event_count(event_type="feed_source_healthy") == 1
    store.record_source_healthy("dexscreener-latest-profile", occurred_at_ms=3050)
    assert store.event_count(event_type="feed_source_healthy") == 1
    assert store.active_outage_count(source="dexscreener-latest-boost") == 1

    store.record_candidate(candidate("dexscreener-latest-boost", 3100))
    assert store.active_outage_count() == 0



def test_store_migrates_existing_outage_table_without_source_column(tmp_path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("""
            CREATE TABLE launch_feed_outages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at_ms INTEGER NOT NULL,
                ended_at_ms INTEGER,
                reason TEXT NOT NULL
            )
        """)
        db.execute(
            "INSERT INTO launch_feed_outages(started_at_ms, reason) VALUES (?, ?)",
            (100, "legacy outage"),
        )

    store = LaunchStore(database)
    assert store.active_outage_count(source="pumpportal-new-token") == 1
    store.record_source_healthy("pumpportal-new-token", occurred_at_ms=200)
    assert store.active_outage_count() == 0

def test_candidate_observation_closes_only_its_own_source_outage(tmp_path):
    store = LaunchStore(tmp_path / "coverage.sqlite3")
    store.record_outage("primary stream down", source="pumpportal-new-token",
                        occurred_at_ms=2000)
    store.record_outage("secondary source down", source="dexscreener-latest-profile",
                        occurred_at_ms=2000)
    store.record_candidate(candidate("pumpportal-new-token", 3000))

    assert store.active_outage_count(source="pumpportal-new-token") == 0
    assert store.active_outage_count(source="dexscreener-latest-profile") == 1


@pytest.mark.parametrize("limit", [0, -1, True, 1001])
def test_candidate_list_rejects_invalid_limits(tmp_path, limit):
    store = LaunchStore(tmp_path / "validation.sqlite3")
    with pytest.raises(ValueError):
        store.list_candidates(limit=limit)


@pytest.mark.parametrize("timestamp", [0, -1, True, "100"])
def test_outage_rejects_invalid_timestamps(tmp_path, timestamp):
    store = LaunchStore(tmp_path / "validation.sqlite3")
    with pytest.raises(ValueError):
        store.record_outage("offline", occurred_at_ms=timestamp)


def test_stream_manager_records_candidate_and_stops_cleanly(tmp_path):
    stop = threading.Event()
    store = LaunchStore(tmp_path / "manager.sqlite3")

    class OneCandidateAdapter:
        name = "pumpportal-new-token"

        def stream_new_tokens(self, *, stop_event=None):
            yield candidate(timestamp=4000)
            stop.set()

    manager = LaunchStreamManager(OneCandidateAdapter(), store, clock_ms=lambda: 5000)
    manager.run(stop)

    assert store.candidate_count() == 1
    assert store.active_outage_count() == 0
    assert store.event_count(event_type="feed_connection_attempt") == 1


def test_stream_manager_records_outage_and_uses_bounded_backoff(tmp_path):
    store = LaunchStore(tmp_path / "manager.sqlite3")

    class EmptyAdapter:
        name = "pumpportal-new-token"

        def stream_new_tokens(self, *, stop_event=None):
            return iter(())

    class StopAfterWait:
        def __init__(self):
            self.stopped = False
            self.delays = []

        def is_set(self):
            return self.stopped

        def wait(self, delay):
            self.delays.append(delay)
            self.stopped = True
            return True

    stop = StopAfterWait()
    manager = LaunchStreamManager(
        EmptyAdapter(), store, initial_backoff_seconds=2, max_backoff_seconds=8,
        clock_ms=lambda: 5000,
    )
    manager.run(stop)

    assert stop.delays == [2]
    assert store.active_outage_count(source="pumpportal-new-token") == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial_backoff_seconds": 0, "max_backoff_seconds": 1},
        {"initial_backoff_seconds": 2, "max_backoff_seconds": 1},
        {"initial_backoff_seconds": True, "max_backoff_seconds": 2},
    ],
)
def test_stream_manager_rejects_invalid_backoff(tmp_path, kwargs):
    store = LaunchStore(tmp_path / "manager.sqlite3")
    with pytest.raises(ValueError):
        LaunchStreamManager(object(), store, **kwargs)
