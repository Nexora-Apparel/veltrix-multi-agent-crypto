from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketData, SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.market_refresh import MarketRefreshWorker

MINT = "So11111111111111111111111111111111111111112"


def launch():
    return normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token",
        observed_at_ms=1_000,
    )


def event(observed, price=0.001):
    return MarketEvent(
        chain="solana", token_address=MINT, pair_address="pool-1",
        symbol="MEME", price_usd=price, liquidity_usd=5000,
        volume_h1_usd=1200, price_change_m5_pct=1.2,
        observed_at_ms=observed, pair_created_at_ms=9000,
        source="dexscreener",
    )


class RotatingProvider:
    def __init__(self):
        self.observed = 10_000
        self.price = 0.001
        self.fail = False
        self.calls = 0

    def fetch_token_pairs(self, chain, token_addresses):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider unavailable")
        return [event(self.observed, self.price)]


def seed_snapshot(store):
    store.record(SolanaMarketSnapshot(
        mint=MINT, selected_pair_address="seed-pool", symbol="MEME",
        price_usd=0.0009, selected_pool_liquidity_usd=4500,
        selected_pool_volume_h1_usd=900, price_change_m5_pct=0.5,
        pair_count=1, retrieved_at_ms=9_000, provider_observed_at_ms=9_000,
        pair_created_at_ms=8_000, quality_flags=(),
    ))


def setup(tmp_path, *, interval=1000, now=10_000, **kwargs):
    path = tmp_path / "shared.sqlite3"
    launches = LaunchStore(path)
    market_store = MarketSnapshotStore(path)
    provider = RotatingProvider()
    worker = MarketRefreshWorker(
        launches, SolanaMarketData(provider), market_store,
        refresh_interval_ms=interval, clock_ms=lambda: now, **kwargs,
    )
    return launches, market_store, provider, worker


def test_refresh_worker_persists_repeated_observations(tmp_path):
    launches, store, provider, worker = setup(tmp_path)
    launches.record_candidate(launch())
    seed_snapshot(store)

    first = worker.run_once()
    assert (first.discovered, first.refreshed, first.failed) == (1, 1, 0)
    assert store.count(MINT) == 2

    provider.observed = 11_000
    provider.price = 0.0015
    worker.clock_ms = lambda: 11_000
    second = worker.run_once()
    assert (second.processed, second.refreshed, second.failed) == (1, 1, 0)
    assert store.count(MINT) == 3
    assert store.latest(MINT)["price_usd"] == 0.0015
    assert provider.calls == 2


def test_refresh_worker_respects_schedule_and_deduplicates_provider_observations(tmp_path):
    launches, store, provider, worker = setup(tmp_path)
    launches.record_candidate(launch())
    seed_snapshot(store)
    worker.run_once()
    scheduled = worker.run_once()
    assert scheduled.processed == 0
    assert store.count(MINT) == 2

    worker.clock_ms = lambda: 11_000
    duplicate = worker.run_once()
    assert duplicate.processed == 1
    assert duplicate.unchanged == 1
    assert store.count(MINT) == 2


def test_refresh_worker_retries_provider_failure_with_backoff(tmp_path):
    launches, store, provider, worker = setup(tmp_path, retry_base_ms=500, retry_max_ms=1000)
    launches.record_candidate(launch())
    seed_snapshot(store)
    provider.fail = True
    first = worker.run_once()
    assert first.failed == 1
    assert worker.status()["retry"] == 1

    worker.clock_ms = lambda: 10_499
    early = worker.run_once()
    assert early.processed == 0

    worker.clock_ms = lambda: 10_500
    provider.fail = False
    provider.observed = 10_500
    retry = worker.run_once()
    assert retry.refreshed == 1
    assert worker.status()["retry"] == 0
    assert worker.status()["degraded"] == 0


def test_refresh_worker_recovers_expired_lease(tmp_path):
    launches, _, provider, worker = setup(tmp_path, lease_ms=100)
    launches.record_candidate(launch())
    seed_snapshot(worker.market_store)
    worker._discover_jobs(10_000)
    claimed = worker._claim(10_000, 1)
    assert claimed == [MINT]

    worker.clock_ms = lambda: 10_101
    recovered = worker.run_once()
    assert recovered.processed == 1
    assert provider.calls == 1


def test_refresh_worker_rejects_split_database_and_invalid_limits(tmp_path):
    launches = LaunchStore(tmp_path / "launch.sqlite3")
    market = MarketSnapshotStore(tmp_path / "market.sqlite3")
    try:
        MarketRefreshWorker(launches, SolanaMarketData(), market)
        assert False, "expected split database to be rejected"
    except ValueError as exc:
        assert "share one persistent" in str(exc)

    launches, _, _, worker = setup(tmp_path)
    for invalid in (0, -1, 501, True):
        try:
            worker.run_once(limit=invalid)
            assert False, "expected invalid limit to be rejected"
        except ValueError:
            pass
