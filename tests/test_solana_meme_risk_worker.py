from dataclasses import replace

from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceReport
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore
from veltrix.solana_meme.risk_worker import SolanaRiskEvidenceWorker

MINT = "So11111111111111111111111111111111111111112"


def report(now, missing=("developer_wallet_linkage",)):
    return SolanaRiskEvidenceReport(
        mint=MINT, observed_at_ms=now,
        goplus_provider="goplus", goplus_evidence_id="gp:1",
        goplus_observed_at_ms=now, liquidity_locked=True,
        holder_provider="rpc", holder_evidence_id="holders:1",
        holder_block_ref="finalized-slot:1", holder_count=1,
        total_supply_raw=100, observed_holder_balance_raw=100,
        holder_coverage_complete=True, top_holder_pct=100.0,
        top5_holder_pct=100.0, top10_holder_pct=100.0,
        missing_claims=missing, errors=(),
    )


class FakeCollector:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def collect(self, mint, *, now_ms):
        self.calls += 1
        if self.fail:
            raise RuntimeError("collector offline")
        return report(now_ms)


def setup(tmp_path, *, now=10_000, interval=1000, **kwargs):
    path = tmp_path / "risk.sqlite3"
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    store = SolanaRiskEvidenceStore(path)
    collector = FakeCollector()
    worker = SolanaRiskEvidenceWorker(
        launches, collector, store, refresh_interval_ms=interval,
        clock_ms=lambda: now, **kwargs,
    )
    return launches, markets, store, collector, worker


def add_candidate(launches, markets):
    candidate = normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token", observed_at_ms=1_000,
    )
    launches.record_candidate(candidate)
    markets.record(SolanaMarketSnapshot(
        mint=MINT, selected_pair_address="pool", symbol="MEME",
        price_usd=0.001, selected_pool_liquidity_usd=5000,
        selected_pool_volume_h1_usd=1000, price_change_m5_pct=1.0,
        pair_count=1, retrieved_at_ms=9_000, provider_observed_at_ms=9_000,
        pair_created_at_ms=8_000, quality_flags=(),
    ))


def test_worker_persists_partial_reports_and_refreshes_on_schedule(tmp_path):
    launches, markets, store, collector, worker = setup(tmp_path)
    add_candidate(launches, markets)
    first = worker.run_once()
    assert (first.discovered, first.processed, first.stored, first.failed) == (1, 1, 1, 0)
    latest = store.latest(MINT)
    assert latest["risk_state"] == "REQUIRES_MORE_EVIDENCE"
    assert "developer_wallet_linkage" in latest["missing_claims"]

    worker.clock_ms = lambda: 10_999
    early = worker.run_once()
    assert early.processed == 0
    worker.clock_ms = lambda: 11_000
    refreshed = worker.run_once()
    assert refreshed.stored == 1
    assert store.count(MINT) == 2
    assert collector.calls == 2


def test_worker_retries_exceptions_with_backoff(tmp_path):
    launches, markets, store, collector, worker = setup(
        tmp_path, retry_base_ms=500, retry_max_ms=1000,
    )
    add_candidate(launches, markets)
    collector.fail = True
    first = worker.run_once()
    assert first.failed == 1
    assert worker.status()["retry"] == 1

    worker.clock_ms = lambda: 10_499
    assert worker.run_once().processed == 0
    worker.clock_ms = lambda: 10_500
    collector.fail = False
    assert worker.run_once().stored == 1
    assert worker.status()["retry"] == 0


def test_worker_rejects_split_database_and_invalid_batch(tmp_path):
    launches = LaunchStore(tmp_path / "launch.sqlite3")
    store = SolanaRiskEvidenceStore(tmp_path / "risk.sqlite3")
    try:
        SolanaRiskEvidenceWorker(launches, FakeCollector(), store)
        assert False, "expected split database to be rejected"
    except ValueError as exc:
        assert "share one SQLite database" in str(exc)

    launches, _, store, collector, worker = setup(tmp_path)
    for invalid in (0, -1, 101, True):
        try:
            worker.run_once(limit=invalid)
            assert False, "expected invalid limit to be rejected"
        except ValueError:
            pass


def test_risk_store_is_append_only_and_deduplicates_identical_reports(tmp_path):
    store = SolanaRiskEvidenceStore(tmp_path / "risk-store.sqlite3")
    item = report(10_000)
    assert store.record(item) is True
    assert store.record(item) is False
    assert store.record(replace(item, observed_at_ms=11_000)) is True
    assert store.count(MINT) == 2
    assert len(store.history(MINT)) == 2
    assert store.latest(MINT)["report"]["observed_at_ms"] == 11_000
