from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.market_refresh import MarketRefreshWorker

MINT = "So11111111111111111111111111111111111111112"


def launch():
    return normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token",
        observed_at_ms=1000,
    )


def event(observed, price=0.001):
    return MarketEvent(
        chain="solana", token_address=MINT, pair_address="pool-1",
        symbol="MEME", name="Meme Token", price_usd=price, liquidity_usd=5000,
        volume_h1_usd=1200, price_change_m5_pct=1.2,
        observed_at_ms=observed, pair_created_at_ms=9000,
        source="dexscreener", fdv_usd=100_000, market_cap_usd=90_000,
        volume_m5_usd=100, volume_h24_usd=10_000,
        price_change_h1_pct=1.0, price_change_h24_pct=5.0,
        txns_m5_buys=10, txns_m5_sells=5,
        txns_h1_buys=100, txns_h1_sells=50,
        txns_h24_buys=1000, txns_h24_sells=800,
    )


class RotatingProvider:
    def __init__(self):
        self.observed = 10_000
        self.price = 0.001
        self.fail = False
        self.calls = []

    def fetch_token_pairs(self, chain, token_addresses):
        self.calls.append((chain, list(token_addresses)))
        if self.fail:
            raise RuntimeError("provider unavailable")
        return [event(self.observed, self.price)]


def seed_snapshot(store):
    store.record(SolanaMarketData(
        RotatingProvider(), clock_ms=lambda: 9_000
    ).fetch(MINT, now_ms=9_000))


def setup(tmp_path, *, interval=1000, now=10_000, **kwargs):
    path = tmp_path / "shared.sqlite3"
    launches = LaunchStore(path)
    market_store = MarketSnapshotStore(path)
    provider = RotatingProvider()
    worker = MarketRefreshWorker(
        launches, SolanaMarketData(provider, clock_ms=lambda: now), market_store,
        refresh_interval_ms=interval, clock_ms=lambda: now, **kwargs,
    )
    return launches, market_store, provider, worker


def test_refresh_worker_persists_repeated_batched_observations(tmp_path):
    launches, store, provider, worker = setup(tmp_path)
    launches.record_candidate(launch())
    seed_snapshot(store)

    first = worker.run_once()
    assert (first.discovered, first.refreshed, first.failed) == (1, 1, 0)
    assert store.count(MINT) == 2

    provider.observed = 11_000
    provider.price = 0.0015
    worker.clock_ms = lambda: 11_000
    worker.market_data.clock_ms = lambda: 11_000
    second = worker.run_once()
    assert (second.processed, second.refreshed, second.failed) == (1, 1, 0)
    assert store.count(MINT) == 3
    assert store.latest(MINT)["price_usd"] == 0.0015
    assert len(provider.calls) == 2


def test_refresh_worker_respects_schedule_and_deduplicates_provider_observations(tmp_path):
    launches, store, provider, worker = setup(tmp_path)
    launches.record_candidate(launch())
    seed_snapshot(store)
    worker.run_once()
    scheduled = worker.run_once()
    assert scheduled.processed == 0
    assert store.count(MINT) == 2

    worker.clock_ms = lambda: 11_000
    worker.market_data.clock_ms = lambda: 11_000
    provider.observed = 11_000
    duplicate = worker.run_once()
    assert duplicate.processed == 1
    assert duplicate.unchanged == 1
    assert store.count(MINT) == 2


def test_refresh_worker_retries_provider_failure_with_backoff(tmp_path):
    launches, store, provider, worker = setup(
        tmp_path, retry_base_ms=500, retry_max_ms=1000
    )
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
    worker.market_data.clock_ms = lambda: 10_500
    provider.fail = False
    provider.observed = 10_500
    retry = worker.run_once()
    assert retry.refreshed == 1
    assert worker.status()["retry"] == 0
    assert worker.status()["degraded"] == 0


def test_refresh_worker_recovers_expired_lease(tmp_path):
    launches, store, provider, worker = setup(tmp_path, lease_ms=100)
    launches.record_candidate(launch())
    seed_snapshot(store)
    worker._discover_jobs(10_000)
    claimed = worker._claim(10_000, 1)
    assert claimed == [MINT]

    worker.clock_ms = lambda: 10_101
    worker.market_data.clock_ms = lambda: 10_101
    provider.observed = 10_101
    recovered = worker.run_once()
    assert recovered.processed == 1
    assert provider.calls == [("solana", [MINT])]


def test_refresh_worker_does_not_queue_candidates_without_initial_snapshot(tmp_path):
    launches, store, provider, worker = setup(tmp_path)
    launches.record_candidate(launch())
    result = worker.run_once()
    assert result.discovered == 0
    assert result.processed == 0
    assert provider.calls == []


def test_refresh_worker_rejects_split_database_and_invalid_limits(tmp_path):
    launches = LaunchStore(tmp_path / "launch.sqlite3")
    market = MarketSnapshotStore(tmp_path / "market.sqlite3")
    try:
        MarketRefreshWorker(launches, SolanaMarketData(), market)
        assert False, "expected split database to be rejected"
    except ValueError as exc:
        assert "share one persistent" in str(exc)

    launches, _, _, worker = setup(tmp_path)
    for invalid in (0, -1, 301, True, 1.5):
        try:
            worker.run_once(limit=invalid)
            assert False, "expected invalid limit to be rejected"
        except ValueError:
            pass
