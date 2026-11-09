import json
import threading

from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.launch_stream import LaunchStore, LaunchStreamManager

MINT = "So11111111111111111111111111111111111111112"


def candidate(source="feed-a", timestamp=1000):
    return normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source=source,
        observed_at_ms=timestamp,
    )


def test_store_persists_and_deduplicates_candidate_sources(tmp_path):
    store = LaunchStore(tmp_path / "launches.sqlite3")
    store.record_candidate(candidate("pumpportal", 1000))
    store.record_candidate(candidate("secondary-feed", 1200))

    assert store.candidate_count() == 1
    import sqlite3
    with sqlite3.connect(tmp_path / "launches.sqlite3") as db:
        row = db.execute(
            "SELECT first_seen_at_ms, latest_seen_at_ms, sources_json "
            "FROM solana_launch_candidates WHERE mint = ?", (MINT,)
        ).fetchone()
    assert row[0:2] == (1000, 1200)
    assert json.loads(row[2]) == ["pumpportal", "secondary-feed"]


def test_outage_is_open_until_a_candidate_is_observed(tmp_path):
    store = LaunchStore(tmp_path / "coverage.sqlite3")
    store.record_outage("socket disconnected", occurred_at_ms=2000)
    store.record_outage("duplicate outage report", occurred_at_ms=2100)
    assert store.active_outage_count() == 1

    store.record_candidate(candidate(timestamp=3000))
    assert store.active_outage_count() == 0


def test_manager_stops_cleanly_after_candidate(tmp_path):
    stop = threading.Event()
    store = LaunchStore(tmp_path / "manager.sqlite3")

    class OneCandidateAdapter:
        def stream_new_tokens(self, *, stop_event=None):
            yield candidate(timestamp=4000)
            stop.set()

    manager = LaunchStreamManager(
        OneCandidateAdapter(), store, clock_ms=lambda: 5000
    )
    manager.run(stop)

    assert store.candidate_count() == 1
    assert store.active_outage_count() == 0


def test_backoff_configuration_is_validated(tmp_path):
    import pytest
    store = LaunchStore(tmp_path / "validation.sqlite3")
    with pytest.raises(ValueError):
        LaunchStreamManager(object(), store, initial_backoff_seconds=2,
                            max_backoff_seconds=1)
