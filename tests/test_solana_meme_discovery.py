import pytest

from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.dexscreener import DexScreenerBatch
from veltrix.solana_meme.discovery import SolanaDiscoveryEngine
from veltrix.solana_meme.launch_stream import LaunchStore

MINT = "So11111111111111111111111111111111111111112"


class FakeSecondarySource:
    name = "dexscreener"

    def __init__(self, batch=None, error=None):
        self.batch = batch
        self.error = error

    def fetch_candidates(self):
        if self.error:
            raise self.error
        return self.batch


def test_poll_once_persists_candidates_and_tracks_source_recovery(tmp_path):
    store = LaunchStore(tmp_path / "discovery.sqlite3")
    store.record_outage(
        "profile feed offline",
        source="dexscreener-latest-profile",
        occurred_at_ms=100,
    )
    candidate = normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="dexscreener-latest-profile",
        observed_at_ms=200,
    )
    source = FakeSecondarySource(DexScreenerBatch(
        candidates=(candidate,),
        successful_sources=("dexscreener-latest-profile", "dexscreener-latest-boost"),
        errors=(),
    ))
    engine = SolanaDiscoveryEngine(
        store, source, poll_interval_seconds=30, clock_ms=lambda: 300,
    )

    result = engine.poll_once()

    assert result.candidates_received == 1
    assert result.candidates_stored == 1
    assert result.healthy_sources == (
        "dexscreener-latest-profile", "dexscreener-latest-boost",
    )
    assert store.candidate_count() == 1
    assert store.active_outage_count(source="dexscreener-latest-profile") == 0


def test_poll_once_records_partial_source_failures(tmp_path):
    store = LaunchStore(tmp_path / "discovery.sqlite3")
    source = FakeSecondarySource(DexScreenerBatch(
        candidates=(),
        successful_sources=("dexscreener-latest-boost",),
        errors=(("dexscreener-latest-profile", "HTTP 503"),),
    ))
    engine = SolanaDiscoveryEngine(store, source, clock_ms=lambda: 300)

    result = engine.poll_once()

    assert result.healthy_sources == ("dexscreener-latest-boost",)
    assert result.source_errors == (("dexscreener-latest-profile", "HTTP 503"),)
    assert store.active_outage_count(source="dexscreener-latest-profile") == 1


def test_poll_once_records_unexpected_secondary_source_failure(tmp_path):
    store = LaunchStore(tmp_path / "discovery.sqlite3")
    engine = SolanaDiscoveryEngine(
        store, FakeSecondarySource(error=RuntimeError("provider failed")),
        clock_ms=lambda: 300,
    )

    result = engine.poll_once()

    assert result.candidates_received == 0
    assert result.source_errors[0][0] == "dexscreener"
    assert store.active_outage_count(source="dexscreener") == 1


@pytest.mark.parametrize("interval", [0, 4, 3601, True, "30"])
def test_engine_rejects_invalid_poll_intervals(tmp_path, interval):
    with pytest.raises(ValueError):
        SolanaDiscoveryEngine(
            LaunchStore(tmp_path / "discovery.sqlite3"),
            FakeSecondarySource(DexScreenerBatch((), (), ())),
            poll_interval_seconds=interval,
        )
