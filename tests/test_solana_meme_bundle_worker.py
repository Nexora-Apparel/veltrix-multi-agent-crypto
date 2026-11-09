from veltrix.launch_bundles import LaunchBundleAnalyzer
from veltrix.launch_intelligence import LaunchCoverage, LaunchIndexSnapshot
from veltrix.solana_meme.bundle_store import LaunchBundleEvidenceStore
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.bundle_worker import SolanaLaunchBundleWorker

MINT = "So11111111111111111111111111111111111111112"
NOW = 2_000_000


class Provider:
    provider_id = "helius-solana-launch-index"
    def __init__(self):
        self.complete = True
    def fetch_launch_snapshot(self, chain, mint, window):
        coverage = LaunchCoverage(
            self.provider_id, chain, mint, NOW - 120_000, NOW,
            NOW, 1, 1, self.complete, "scan-proof",
        )
        return LaunchIndexSnapshot(
            self.provider_id, chain, mint, NOW - 120_000, 1000.0, 6, (),
            coverage, NOW, "finalized-slot:10",
        )


def setup(tmp_path):
    path = tmp_path / "bundle.sqlite3"
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    store = LaunchBundleEvidenceStore(path)
    provider = Provider()
    analyzer = LaunchBundleAnalyzer(
        trusted_providers=(provider.provider_id,),
        trusted_funder_providers=("helius-solana-funding-graph",),
        launch_window_ms=120_000,
    )
    worker = SolanaLaunchBundleWorker(
        launches, provider, analyzer, store, clock_ms=lambda: NOW,
    )
    return launches, markets, store, provider, worker


def add_candidate(launches, markets):
    launches.record_candidate(normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token", observed_at_ms=NOW - 200_000,
    ))
    markets.record(SolanaMarketSnapshot(
        mint=MINT, selected_pair_address="pool", symbol="MEME",
        price_usd=0.001, selected_pool_liquidity_usd=5000,
        selected_pool_volume_h1_usd=1000, price_change_m5_pct=1.0,
        pair_count=1, retrieved_at_ms=NOW - 100_000, provider_observed_at_ms=NOW - 100_000,
        pair_created_at_ms=NOW - 200_000, quality_flags=(),
    ))


def test_worker_persists_complete_window_even_when_no_shared_funder_cluster(tmp_path):
    launches, markets, store, provider, worker = setup(tmp_path)
    add_candidate(launches, markets)
    result = worker.run_once()
    assert result.stored == 1
    assert result.complete == 1
    saved = store.latest(MINT)["report"]
    assert saved["bundled_supply_pct"] == 0
    assert saved["shared_funder_clusters"] == 0
    assert saved["evidence_id"] == "scan-proof"


def test_worker_withholds_percentage_when_window_coverage_is_incomplete(tmp_path):
    launches, markets, store, provider, worker = setup(tmp_path)
    add_candidate(launches, markets)
    provider.complete = False
    result = worker.run_once()
    assert result.stored == 1
    assert result.incomplete == 1
    assert store.latest(MINT)["report"]["bundled_supply_pct"] is None


def test_worker_rejects_split_database_and_invalid_batch(tmp_path):
    launches = LaunchStore(tmp_path / "a.sqlite3")
    store = LaunchBundleEvidenceStore(tmp_path / "b.sqlite3")
    provider = Provider()
    analyzer = LaunchBundleAnalyzer((provider.provider_id,), launch_window_ms=120_000)
    try:
        SolanaLaunchBundleWorker(launches, provider, analyzer, store)
        assert False, "expected split database rejection"
    except ValueError as exc:
        assert "share one SQLite database" in str(exc)

    launches, _, store, provider, worker = setup(tmp_path)
    for invalid in (0, -1, 11, True):
        try:
            worker.run_once(limit=invalid)
            assert False, "expected invalid batch rejection"
        except ValueError:
            pass


def test_store_is_append_only_and_deduplicates(tmp_path):
    launches, markets, store, provider, worker = setup(tmp_path)
    add_candidate(launches, markets)
    worker.run_once()
    report = worker._collect(MINT, NOW)
    assert store.record(report) is False
    assert store.count(MINT) == 1
