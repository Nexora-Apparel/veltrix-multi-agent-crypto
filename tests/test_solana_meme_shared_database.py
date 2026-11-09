from types import SimpleNamespace

from veltrix.solana_meme.consensus_runtime import build_runtime as build_consensus_runtime
from veltrix.solana_meme.intelligence_worker import build_runtime as build_market_runtime
from veltrix.solana_meme.paper_runtime import build_runtime as build_paper_runtime
from veltrix.solana_meme.risk_runtime import build_runtime as build_risk_runtime
from veltrix.solana_meme.worker import build_engine


class FakeCollector:
    def collect(self, mint, now_ms=None):
        raise AssertionError("preflight must not collect evidence")


class FakeMarketProvider:
    def fetch_token_pairs(self, chain, token_addresses):
        return []


def test_all_solana_workers_resolve_to_one_legacy_db_path(tmp_path, monkeypatch):
    database = tmp_path / "shared-pipeline.sqlite3"
    monkeypatch.setenv("DB_PATH", str(database))
    monkeypatch.setenv("VELTRIX_SOLANA_DISCOVERY_DB", "")
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("VELTRIX_TRUSTED_SOLANA_WALLETS", raising=False)
    monkeypatch.delenv("TRACKED_SMART_WALLETS", raising=False)
    monkeypatch.delenv("TRUSTED_WALLET_PROVIDERS", raising=False)

    discovery = build_engine(websocket_factory=lambda *args, **kwargs: None)
    market = build_market_runtime(provider=FakeMarketProvider())
    risk = build_risk_runtime(
        settings_obj=SimpleNamespace(
            solana_rpc_url="", helius_api_key="", goplus_access_token="",
        ),
        collector=FakeCollector(),
    )
    consensus = build_consensus_runtime()
    paper = build_paper_runtime()

    paths = {
        discovery.store.database_path,
        market.enrichment.launch_store.database_path,
        market.refresh.market_store.database_path,
        risk.worker.database_path,
        consensus.worker.database_path,
        paper.worker.broker.db_path,
    }
    assert paths == {str(database)}
