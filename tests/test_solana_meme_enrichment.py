import pytest

from veltrix.providers.base import MarketEvent, ProviderError
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.enrichment import CandidateEnrichmentWorker
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore

MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def launch(mint, observed=1000):
    return normalize_candidate(
        {"chain": "solana", "mint": mint},
        source="pumpportal-new-token",
        observed_at_ms=observed,
    )


def market_event(mint, observed=10_000):
    return MarketEvent(
        chain="solana", token_address=mint, pair_address=f"pool-{mint[-4:]}",
        symbol="MEME", name="Meme Token", price_usd=0.001,
        liquidity_usd=5000, volume_h1_usd=1200, price_change_m5_pct=1.2,
        observed_at_ms=observed, pair_created_at_ms=9000, source="dexscreener",
        fdv_usd=100_000, market_cap_usd=90_000,
        volume_m5_usd=100, volume_h24_usd=10_000,
        price_change_h1_pct=2.0, price_change_h24_pct=10.0,
        txns_m5_buys=10, txns_m5_sells=5,
        txns_h1_buys=100, txns_h1_sells=60,
        txns_h24_buys=900, txns_h24_sells=700,
    )


class FakeMarketProvider:
    def __init__(self, events=None, fail=False):
        self.events = list(events or [])
        self.fail = fail
        self.calls = []

    def fetch_token_pairs(self, chain, token_addresses):
        self.calls.append((chain, list(token_addresses)))
        if self.fail:
            raise ProviderError("provider unavailable")
        return self.events


def setup_worker(tmp_path, *, provider=None, now=10_000, **kwargs):
    path = tmp_path / "shared.sqlite3"
    launches = LaunchStore(path)
    snapshots = MarketSnapshotStore(path)
    provider = provider or FakeMarketProvider([market_event(MINT_A, now)])
    market = SolanaMarketData(provider, clock_ms=lambda: now)
    worker = CandidateEnrichmentWorker(
        launches, market, snapshots, clock_ms=lambda: now, **kwargs
    )
    return launches, snapshots, provider, worker


def test_worker_batches_discovered_mints_and_persists_market_snapshots(tmp_path):
    provider = FakeMarketProvider([
        market_event(MINT_A),
        market_event(MINT_B),
    ])
    launches, snapshots, provider, worker = setup_worker(tmp_path, provider=provider)
    launches.record_candidate(launch(MINT_A))
    launches.record_candidate(launch(MINT_B))

    result = worker.run_once(limit=25)

    assert (result.discovered, result.processed, result.enriched, result.failed) == (2, 2, 2, 0)
    assert snapshots.count() == 2
    assert worker.status()["completed"] == 2
    assert provider.calls == [("solana", sorted([MINT_A, MINT_B]))]


def test_completed_candidate_is_not_reprocessed(tmp_path):
    launches, snapshots, provider, worker = setup_worker(tmp_path)
    launches.record_candidate(launch(MINT_A))

    worker.run_once()
    second = worker.run_once()

    assert second.processed == 0
    assert snapshots.count(MINT_A) == 1
    assert len(provider.calls) == 1


def test_provider_failure_is_retried_with_backoff(tmp_path):
    provider = FakeMarketProvider(fail=True)
    launches, _, provider, worker = setup_worker(
        tmp_path, provider=provider, retry_base_ms=5000, retry_max_ms=10000
    )
    launches.record_candidate(launch(MINT_A))

    first = worker.run_once()
    assert first.failed == 1
    assert worker.status()["retry"] == 1

    second = worker.run_once()
    assert second.processed == 0
    assert len(provider.calls) == 1


def test_missing_market_data_fails_only_the_unavailable_mint(tmp_path):
    provider = FakeMarketProvider([market_event(MINT_A)])
    launches, snapshots, _, worker = setup_worker(tmp_path, provider=provider)
    launches.record_candidate(launch(MINT_A))
    launches.record_candidate(launch(MINT_B))

    result = worker.run_once()

    assert result.enriched == 1
    assert result.failed == 1
    assert snapshots.count(MINT_A) == 1
    assert snapshots.count(MINT_B) == 0
    assert worker.status()["completed"] == 1
    assert worker.status()["retry"] == 1


def test_worker_marks_job_dead_after_max_attempts(tmp_path):
    provider = FakeMarketProvider(fail=True)
    launches, _, _, worker = setup_worker(
        tmp_path, provider=provider, max_attempts=1
    )
    launches.record_candidate(launch(MINT_A))
    result = worker.run_once()
    assert result.failed == 1
    assert worker.status()["dead"] == 1


def test_worker_recovers_expired_processing_lease(tmp_path):
    launches, snapshots, provider, worker = setup_worker(
        tmp_path, lease_ms=100, now=10_000
    )
    launches.record_candidate(launch(MINT_A))
    worker._discover_jobs(10_000)
    assert worker._claim(10_000, 1) == [MINT_A]

    worker.clock_ms = lambda: 10_101
    worker.market_data.clock_ms = lambda: 10_101
    provider.events = [market_event(MINT_A, 10_101)]
    result = worker.run_once()
    assert result.enriched == 1
    assert snapshots.count(MINT_A) == 1


def test_worker_rejects_split_or_memory_databases(tmp_path):
    launch_store = LaunchStore(tmp_path / "launch.sqlite3")
    market_store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    with pytest.raises(ValueError, match="share one persistent"):
        CandidateEnrichmentWorker(
            launch_store, SolanaMarketData(FakeMarketProvider()), market_store
        )

    memory_launches = LaunchStore(":memory:")
    persistent_market = MarketSnapshotStore(tmp_path / "persistent.sqlite3")
    with pytest.raises(ValueError, match="share one persistent"):
        CandidateEnrichmentWorker(
            memory_launches, SolanaMarketData(FakeMarketProvider()), persistent_market
        )


@pytest.mark.parametrize("limit", [0, -1, 301, True, 1.5])
def test_worker_rejects_invalid_batch_size(tmp_path, limit):
    launches, _, _, worker = setup_worker(tmp_path)
    with pytest.raises(ValueError):
        worker.run_once(limit=limit)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": True},
        {"lease_ms": 0},
        {"retry_base_ms": 0},
        {"retry_max_ms": 1, "retry_base_ms": 2},
    ],
)
def test_worker_rejects_invalid_retry_configuration(tmp_path, kwargs):
    path = tmp_path / "config.sqlite3"
    launches = LaunchStore(path)
    snapshots = MarketSnapshotStore(path)
    with pytest.raises(ValueError):
        CandidateEnrichmentWorker(
            launches, SolanaMarketData(FakeMarketProvider()), snapshots, **kwargs
        )
