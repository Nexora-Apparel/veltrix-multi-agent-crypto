import pytest

from veltrix.liquidity_evidence import LiquidityInventoryBatch, LiquidityEvidenceAnalyzer
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.liquidity_store import SolanaLiquidityEvidenceStore
from veltrix.solana_meme.liquidity_worker import SolanaLiquidityEvidenceWorker

MINT = "So11111111111111111111111111111111111111112"
NOW = 10_000_000


class Provider:
    provider_id = "solana-raydium-v4-cpmm-finalized-pool-inventory"

    def __init__(self):
        self.fail = False

    def fetch_inventory(self, mint):
        if self.fail:
            raise RuntimeError("RPC unavailable")
        return LiquidityInventoryBatch(
            chain="solana", token_address=mint, provider=self.provider_id,
            independence_group="pool-indexer", observed_at_ms=NOW - 100,
            finalized_ref="slot:100:hash", pool_set_id="raydium-v4-cpmm",
            inventory_complete=False, pools=(), positions=(), deployments=(),
        )


def setup(tmp_path):
    path = tmp_path / "liquidity.sqlite3"
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    store = SolanaLiquidityEvidenceStore(path)
    provider = Provider()
    worker = SolanaLiquidityEvidenceWorker(
        launches, provider, store, clock_ms=lambda: NOW,
    )
    return launches, markets, store, provider, worker


def add_candidate(launches, markets):
    launches.record_candidate(normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token", observed_at_ms=NOW - 1_000,
    ))
    markets.record(SolanaMarketSnapshot(
        mint=MINT, selected_pair_address="pool", symbol="MEME",
        price_usd=0.001, selected_pool_liquidity_usd=5000,
        selected_pool_volume_h1_usd=1000, price_change_m5_pct=1.0,
        pair_count=1, retrieved_at_ms=NOW - 500,
        provider_observed_at_ms=NOW - 500,
        pair_created_at_ms=NOW - 1_000, quality_flags=(),
    ))


def test_worker_persists_incomplete_inventory_without_certifying_liquidity(tmp_path):
    launches, markets, store, provider, worker = setup(tmp_path)
    add_candidate(launches, markets)
    result = worker.run_once()
    assert result.discovered == result.processed == result.stored == 1
    assert result.passed == 0
    assert result.failed == 0
    saved_row = store.latest(MINT)
    saved = saved_row["report"]
    assert saved["passed"] is False
    assert saved["pool_count"] == 0
    assert saved_row["inventory"]["finalized_ref"] == "slot:100:hash"
    assert saved_row["inventory"]["provider"] == Provider.provider_id
    assert any("pool inventory is not certified complete" in reason for reason in saved["reasons"])


def test_worker_retries_provider_failures_without_fabricating_evidence(tmp_path):
    launches, markets, store, provider, worker = setup(tmp_path)
    add_candidate(launches, markets)
    provider.fail = True
    result = worker.run_once()
    assert result.failed == 1
    assert store.count(MINT) == 0
    assert worker.status()["retry"] == 1


def test_store_is_append_only_and_deduplicates_reports(tmp_path):
    path = tmp_path / "store.sqlite3"
    store = SolanaLiquidityEvidenceStore(path)
    batch = LiquidityInventoryBatch(
        chain="solana", token_address=MINT, provider="reader",
        independence_group="pool-indexer", observed_at_ms=NOW - 100,
        finalized_ref="slot:100:hash", pool_set_id="raydium-v4-cpmm",
        inventory_complete=False, pools=(), positions=(), deployments=(),
    )
    report = LiquidityEvidenceAnalyzer().evaluate(
        batch, chain="solana", token_address=MINT, now_ms=NOW,
    )
    assert store.record(report) is True
    assert store.record(report) is False
    assert store.count(MINT) == 1
    assert store.latest(MINT)["report"]["passed"] is False


def test_worker_rejects_split_databases_and_invalid_limits(tmp_path):
    launches = LaunchStore(tmp_path / "a.sqlite3")
    store = SolanaLiquidityEvidenceStore(tmp_path / "b.sqlite3")
    try:
        SolanaLiquidityEvidenceWorker(launches, Provider(), store)
        assert False, "expected split database rejection"
    except ValueError as exc:
        assert "share one SQLite database" in str(exc)

    launches, markets, store, provider, worker = setup(tmp_path)
    for invalid in (0, -1, 11, True):
        try:
            worker.run_once(limit=invalid)
            assert False, "expected invalid batch rejection"
        except ValueError:
            pass



def test_store_rejects_report_inventory_identity_mismatch(tmp_path):
    from dataclasses import replace

    path = tmp_path / "mismatch.sqlite3"
    store = SolanaLiquidityEvidenceStore(path)
    batch = LiquidityInventoryBatch(
        chain="solana", token_address=MINT, provider="reader",
        independence_group="pool-indexer", observed_at_ms=NOW - 100,
        finalized_ref="slot:100:hash", pool_set_id="raydium-v4-cpmm",
        inventory_complete=False, pools=(), positions=(), deployments=(),
    )
    report = LiquidityEvidenceAnalyzer().evaluate(
        batch, chain="solana", token_address=MINT, now_ms=NOW,
    )
    wrong_batch = replace(batch, token_address="11111111111111111111111111111111")
    with pytest.raises(ValueError, match="identity mismatch"):
        store.record(report, wrong_batch)
