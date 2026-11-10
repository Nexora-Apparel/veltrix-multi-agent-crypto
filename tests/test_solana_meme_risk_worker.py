from dataclasses import replace

import pytest

from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceCollector
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore
from veltrix.solana_meme.risk_worker import SolanaRiskEvidenceWorker
from veltrix.orchestration import LeaseBusy, OrchestrationStore, RunContext, run_once

MINT = "So11111111111111111111111111111111111111112"


def launch():
    return normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token",
        observed_at_ms=1000,
    )


class MarketProvider:
    def fetch_token_pairs(self, chain, token_addresses):
        now = 9000
        return [MarketEvent(
            chain="solana", token_address=mint, pair_address="pool",
            symbol="MEME", price_usd=0.001, liquidity_usd=1000,
            volume_h1_usd=100, price_change_m5_pct=1.0,
            observed_at_ms=now, pair_created_at_ms=8000, source="dexscreener",
        ) for mint in token_addresses]


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeCollector:
    def __init__(self, clock, fail=False, errors=()):
        self.clock = clock
        self.fail = fail
        self.errors = errors
        self.calls = []
        self.on_collect = None

    def collect(self, mint, now_ms=None):
        self.calls.append((mint, now_ms))
        if self.on_collect is not None:
            self.on_collect()
        if self.fail:
            raise RuntimeError("risk provider offline")
        return SolanaRiskEvidenceCollector(
            None, None, clock_ms=self.clock
        ).collect(mint, now_ms=now_ms) if not self.errors else replace(
            SolanaRiskEvidenceCollector(None, None, clock_ms=self.clock).collect(
                mint, now_ms=now_ms
            ),
            errors=tuple(self.errors),
        )


def setup_worker(tmp_path, *, now=10_000, collector=None, **kwargs):
    path = tmp_path / "shared.sqlite3"
    launches = LaunchStore(path)
    market_store = MarketSnapshotStore(path)
    risk_store = SolanaRiskEvidenceStore(path)
    launches.record_candidate(launch())
    market_store.record(SolanaMarketData(
        MarketProvider(), clock_ms=lambda: 9000
    ).fetch(MINT, now_ms=9000))
    clock = Clock(now)
    collector = collector or FakeCollector(clock)
    worker = SolanaRiskEvidenceWorker(
        launches, market_store, collector, risk_store,
        clock_ms=clock, **kwargs,
    )
    return launches, market_store, risk_store, collector, worker, clock


def test_risk_worker_collects_and_persists_evidence_for_enriched_candidates(tmp_path):
    _, _, risk_store, collector, worker, _ = setup_worker(tmp_path)
    result = worker.run_once()

    assert (result.discovered, result.processed, result.recorded, result.failed) == (1, 1, 1, 0)
    assert risk_store.count(MINT) == 1
    assert risk_store.latest(MINT)["risk_state"] == "REQUIRES_MORE_EVIDENCE"
    assert worker.status()["succeeded"] == 1
    assert collector.calls == [(MINT, 10_000)]


def test_risk_worker_respects_refresh_interval(tmp_path):
    _, _, risk_store, collector, worker, clock = setup_worker(tmp_path)
    worker.run_once()
    second = worker.run_once()
    assert second.processed == 0
    assert len(collector.calls) == 1

    clock.value = 310_000
    third = worker.run_once()
    assert third.processed == 1
    assert risk_store.count(MINT) == 2


def test_risk_worker_retries_collector_exception_with_backoff(tmp_path):
    clock = Clock(10_000)
    collector = FakeCollector(clock, fail=True)
    _, _, _, _, worker, worker_clock = setup_worker(
        tmp_path, collector=collector, retry_base_ms=500, retry_max_ms=1000
    )
    first = worker.run_once()
    assert first.failed == 1
    assert worker.status()["retry"] == 1

    worker_clock.value = 10_499
    assert worker.run_once().processed == 0

    worker_clock.value = 10_500
    collector.fail = False
    retried = worker.run_once()
    assert retried.recorded == 1


def test_risk_worker_records_partial_reports_with_provider_errors(tmp_path):
    clock = Clock(10_000)
    collector = FakeCollector(clock, errors=("mint rpc unavailable",))
    _, _, risk_store, _, worker, _ = setup_worker(tmp_path, collector=collector)
    result = worker.run_once()

    assert result.recorded == 1
    assert risk_store.latest(MINT)["errors"] == ["mint rpc unavailable"]
    assert worker.status()["with_provider_errors"] == 1


def test_risk_worker_ignores_candidates_without_market_snapshots(tmp_path):
    path = tmp_path / "shared.sqlite3"
    launches = LaunchStore(path)
    market_store = MarketSnapshotStore(path)
    risk_store = SolanaRiskEvidenceStore(path)
    launches.record_candidate(launch())
    collector = FakeCollector(Clock(10_000))
    worker = SolanaRiskEvidenceWorker(
        launches, market_store, collector, risk_store,
        clock_ms=lambda: 10_000,
    )
    result = worker.run_once()
    assert result.discovered == 0
    assert result.processed == 0
    assert collector.calls == []


def test_risk_worker_recovers_expired_processing_lease(tmp_path):
    _, _, risk_store, collector, worker, clock = setup_worker(
        tmp_path, lease_ms=100
    )
    worker._discover_jobs(10_000)
    assert worker._claim(10_000, 1) == [MINT]

    clock.value = 10_101
    result = worker.run_once()
    assert result.recorded == 1
    assert risk_store.count(MINT) == 1
    assert len(collector.calls) == 1


def test_risk_worker_requires_one_persistent_database(tmp_path):
    launch_store = LaunchStore(tmp_path / "launch.sqlite3")
    market_store = MarketSnapshotStore(tmp_path / "market.sqlite3")
    risk_store = SolanaRiskEvidenceStore(tmp_path / "risk.sqlite3")
    with pytest.raises(ValueError, match="share one persistent"):
        SolanaRiskEvidenceWorker(
            launch_store, market_store, FakeCollector(Clock(10_000)), risk_store
        )


@pytest.mark.parametrize("limit", [0, -1, 26, True, 1.5])
def test_risk_worker_rejects_invalid_batch_limit(tmp_path, limit):
    _, _, _, _, worker, _ = setup_worker(tmp_path)
    with pytest.raises(ValueError):
        worker.run_once(limit=limit)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"refresh_interval_ms": 0},
        {"lease_ms": 0},
        {"retry_base_ms": 0},
        {"retry_max_ms": 1, "retry_base_ms": 2},
    ],
)
def test_risk_worker_rejects_invalid_configuration(tmp_path, kwargs):
    path = tmp_path / "config.sqlite3"
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    risks = SolanaRiskEvidenceStore(path)
    with pytest.raises(ValueError):
        SolanaRiskEvidenceWorker(
            launches, markets, FakeCollector(Clock(10_000)), risks, **kwargs
        )


def test_risk_worker_pipeline_fences_and_commits_report_with_job_state(tmp_path):
    launches, _, risk_store, _, worker, _ = setup_worker(tmp_path)
    orchestration = OrchestrationStore(str(launches.database_path))
    orchestration.initialize()

    def handler(context, payload):
        result = worker.run_once(context=context)
        assert result.recorded == 1

    result = run_once(
        orchestration, idempotency_key="risk-evidence-window-1",
        job_type="risk_evidence", payload={}, handler=handler,
    )
    assert result["status"] == "SUCCEEDED"
    assert risk_store.count(MINT) == 1
    assert worker.status()["processing"] == 0


def test_risk_worker_rejects_replaced_claim_after_collection(tmp_path):
    launches, _, risk_store, collector, worker, _ = setup_worker(tmp_path)
    context = RunContext(
        "run-1", "key-1", 1, 100.0, 5, lambda: 1.0, 7, lambda: None,
        owner_id="worker-a",
        _assert_fence_in_transaction=lambda db: None,
    )

    def replace_claim():
        with worker._connect() as db:
            db.execute(
                "UPDATE solana_risk_evidence_jobs SET claim_fencing_token=8 WHERE mint=?",
                (MINT,),
            )

    collector.on_collect = replace_claim
    with pytest.raises(LeaseBusy, match="row claim"):
        worker.run_once(context=context)
    assert risk_store.count(MINT) == 0
    assert worker.status()["processing"] == 1
