from dataclasses import dataclass

import pytest

from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.enrichment import CandidateEnrichmentWorker
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore

MINT = "So11111111111111111111111111111111111111112"


def launch():
    return normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token",
        observed_at_ms=1_000,
    )


def market_event(observed=10_000):
    return MarketEvent(
        chain="solana", token_address=MINT, pair_address="pool-1",
        symbol="MEME", price_usd=0.001, liquidity_usd=5000,
        volume_h1_usd=1200, price_change_m5_pct=1.2,
        observed_at_ms=observed, pair_created_at_ms=9000,
        source="dexscreener",
    )


class FakeMarketProvider:
    def __init__(self, events=None, fail=False):
        self.events = events or [market_event()]
        self.fail = fail
        self.calls = []

    def fetch_token_pairs(self, chain, token_addresses):
        self.calls.append((chain, token_addresses))
        if self.fail:
            raise RuntimeError("provider unavailable")
        return self.events


def setup_worker(tmp_path, *, provider=None, now=10_000, **kwargs):
    path = tmp_path / "shared.sqlite3"
    launches = LaunchStore(path)
    snapshots = MarketSnapshotStore(path)
    provider = provider or FakeMarketProvider()
    market = SolanaMarketData(provider)
    worker = CandidateEnrichmentWorker(
        launches, market, snapshots, clock_ms=lambda: now, **kwargs
    )
    return launches, snapshots, provider, worker


def test_worker_discovers_enriches_and_persists_market_snapshot(tmp_path):
    launches, snapshots, provider, worker = setup_worker(tmp_path)
    launches.record_candidate(launch())

    result = worker.run_once()
    assert (result.discovered, result.processed, result.enriched, result.failed) == (1, 1, 1, 0)
    assert snapshots.count(MINT) == 1
    assert worker.status()["completed"] == 1
    assert provider.calls == [("solana", [MINT])]


def test_completed_candidate_is_not_reprocessed(tmp_path):
    launches, snapshots, provider, worker = setup_worker(tmp_path)
    launches.record_candidate(launch())
    worker.run_once()
    second = worker.run_once()
    assert second.processed == 0
    assert snapshots.count(MINT) == 1
    assert len(provider.calls) == 1


def test_provider_failure_is_retried_with_backoff(tmp_path):
    provider = FakeMarketProvider(fail=True)
    launches, _, _, worker = setup_worker(
        tmp_path, provider=provider, retry_base_ms=5000, retry_max_ms=10000
    )
    launches.record_candidate(launch())
    first = worker.run_once()
    assert first.failed == 1
    assert worker.status()["retry"] == 1

    second = worker.run_once()
    assert second.processed == 0


def test_worker_rejects_separate_or_ephemeral_databases(tmp_path):
    launch_store = LaunchStore(tmp_path / "launch.sqlite3")
    market_store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    with pytest.raises(ValueError, match="same persistent"):
        CandidateEnrichmentWorker(launch_store, SolanaMarketData(FakeMarketProvider()),
                                  market_store)
    with pytest.raises(ValueError, match="same persistent"):
        CandidateEnrichmentWorker(LaunchStore(":memory:"), SolanaMarketData(),
                                  MarketSnapshotStore(":memory:"))


def test_worker_rejects_invalid_batch_size(tmp_path):
    launches, snapshots, _, worker = setup_worker(tmp_path)
    with pytest.raises(ValueError):
        worker.run_once(limit=0)
