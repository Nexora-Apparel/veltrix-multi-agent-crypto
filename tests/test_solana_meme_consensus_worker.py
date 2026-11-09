from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.consensus import ConsensusEngine
from veltrix.solana_meme.consensus_store import ConsensusStore
from veltrix.solana_meme.consensus_worker import ConsensusWorker
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.risk_evidence import SolanaRiskEvidenceCollector
from veltrix.solana_meme.risk_store import SolanaRiskEvidenceStore

MINT = "So11111111111111111111111111111111111111112"


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class MarketProvider:
    def fetch_token_pairs(self, chain, token_addresses):
        return [MarketEvent(
            chain="solana", token_address=mint, pair_address="pool",
            symbol="MEME", price_usd=0.001, liquidity_usd=100_000,
            volume_h1_usd=50_000, price_change_m5_pct=2.0,
            observed_at_ms=9000, pair_created_at_ms=8000,
            source="dexscreener",
        ) for mint in token_addresses]


def candidate():
    return normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token",
        observed_at_ms=1000,
    )


def setup_worker(
    tmp_path, *, now=10_000, wallet_event_provider=None,
    retry_base_ms=500, lease_ms=100,
):
    path = tmp_path / "shared.sqlite3"
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    risks = SolanaRiskEvidenceStore(path)
    decisions = ConsensusStore(path)
    launches.record_candidate(candidate())
    markets.record(SolanaMarketData(
        MarketProvider(), clock_ms=lambda: 9000
    ).fetch(MINT, now_ms=9000))
    risks.record(SolanaRiskEvidenceCollector(
        None, None, clock_ms=lambda: now
    ).collect(MINT, now_ms=now))
    clock = Clock(now)
    worker = ConsensusWorker(
        launches, markets, risks, ConsensusEngine(), decisions,
        clock_ms=clock, wallet_event_provider=wallet_event_provider,
        retry_base_ms=retry_base_ms, retry_max_ms=1000,
        lease_ms=lease_ms,
    )
    return launches, markets, risks, decisions, worker, clock


def test_consensus_worker_generates_and_persists_read_only_decision(tmp_path):
    _, _, _, store, worker, _ = setup_worker(tmp_path)
    result = worker.run_once()

    assert (result.discovered, result.processed, result.recorded, result.failed) == (1, 1, 1, 0)
    decision = store.latest(MINT)
    assert decision["status"] == "ABSTAIN"
    assert decision["execution_authorized"] is False
    assert worker.status()["succeeded"] == 1


def test_consensus_worker_respects_refresh_interval(tmp_path):
    _, _, _, store, worker, clock = setup_worker(tmp_path)
    worker.run_once()
    second = worker.run_once()
    assert second.processed == 0
    assert store.count(MINT) == 1

    clock.value = 70_000
    third = worker.run_once()
    assert third.recorded == 1
    assert store.count(MINT) == 2


def test_consensus_worker_retries_wallet_event_provider_errors(tmp_path):
    def failing_provider(_mint):
        raise RuntimeError("wallet feed unavailable")

    _, _, _, store, worker, clock = setup_worker(
        tmp_path, wallet_event_provider=failing_provider
    )
    first = worker.run_once()
    assert first.failed == 1
    assert worker.status()["retry"] == 1
    assert store.count(MINT) == 0

    clock.value = 10_499
    assert worker.run_once().processed == 0
    clock.value = 10_500
    worker.wallet_event_provider = lambda _mint: ()
    retried = worker.run_once()
    assert retried.recorded == 1


def test_consensus_worker_recovers_expired_processing_lease(tmp_path):
    _, _, _, store, worker, clock = setup_worker(tmp_path)
    worker._discover_jobs(10_000)
    assert worker._claim(10_000, 1) == [MINT]

    clock.value = 10_101
    result = worker.run_once()
    assert result.recorded == 1
    assert store.count(MINT) == 1


def test_consensus_worker_requires_market_and_risk_evidence(tmp_path):
    path = tmp_path / "shared.sqlite3"
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    risks = SolanaRiskEvidenceStore(path)
    decisions = ConsensusStore(path)
    launches.record_candidate(candidate())
    worker = ConsensusWorker(
        launches, markets, risks, ConsensusEngine(), decisions,
        clock_ms=lambda: 10_000,
    )
    result = worker.run_once()
    assert result.discovered == 0
    assert result.processed == 0


def test_consensus_worker_rejects_split_databases(tmp_path):
    launches = LaunchStore(tmp_path / "launch.sqlite3")
    markets = MarketSnapshotStore(tmp_path / "market.sqlite3")
    risks = SolanaRiskEvidenceStore(tmp_path / "risk.sqlite3")
    decisions = ConsensusStore(tmp_path / "consensus.sqlite3")
    try:
        ConsensusWorker(
            launches, markets, risks, ConsensusEngine(), decisions
        )
        assert False, "expected split databases to be rejected"
    except ValueError as exc:
        assert "share one persistent" in str(exc)


def test_consensus_worker_rejects_invalid_limit(tmp_path):
    _, _, _, _, worker, _ = setup_worker(tmp_path)
    for invalid in (0, -1, 26, True, 1.5):
        try:
            worker.run_once(limit=invalid)
            assert False, "expected invalid limit to be rejected"
        except ValueError:
            pass
