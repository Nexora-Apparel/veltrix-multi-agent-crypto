from veltrix.developer_concentration import (
    FundingGraphSnapshot, FundingRelationship, HolderBalance, HolderSnapshot,
)
from veltrix.developer_evidence import DeveloperRootAttestation
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.developer_store import DeveloperConcentrationStore
from veltrix.solana_meme.launch_stream import LaunchStore
from veltrix.solana_meme.market_data import SolanaMarketSnapshot
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.developer_worker import SolanaDeveloperEvidenceWorker

MINT = "So11111111111111111111111111111111111111112"
NOW = 10_000_000


class RootProvider:
    provider_id = "signed-roots"
    def fetch_attestation(self, chain, mint):
        return DeveloperRootAttestation(
            chain, mint, self.provider_id, "root-proof", NOW - 100,
            ("dev-wallet",), True, "https://roots.example/proof",
            signature_verified=True, signing_key_fingerprint="a" * 64,
        )


class HolderProvider:
    provider_id = "holder-index"
    def fetch_snapshot(self, mint):
        return HolderSnapshot(
            self.provider_id, "solana", mint, NOW - 100, "holders-1",
            "finalized-slot:1", 6, 1000,
            (HolderBalance("dev-wallet", 100), HolderBalance("holder-wallet", 900)),
            True,
        )


class FundingProvider:
    provider_id = "helius-funding"
    def fetch_snapshot(self, mint, wallets):
        return FundingGraphSnapshot(
            self.provider_id, "solana", mint, NOW - 100, "funding-1",
            "parsed-events:1", 0, NOW - 50,
            ("dev-wallet", "holder-wallet"),
            (
                FundingRelationship("dev-wallet", None, None, NOW - 100, "edge-1"),
                FundingRelationship("holder-wallet", "dev-wallet", "signature-1", NOW - 100, "edge-2"),
            ), True,
        )


def setup(tmp_path, **kwargs):
    path = tmp_path / "dev.sqlite3"
    launches = LaunchStore(path)
    markets = MarketSnapshotStore(path)
    store = DeveloperConcentrationStore(path)
    worker = SolanaDeveloperEvidenceWorker(
        launches, HolderProvider(), FundingProvider(), RootProvider(), store,
        trusted_holder_providers=("holder-index",),
        trusted_funding_providers=("helius-funding",),
        trusted_root_providers=("signed-roots",),
        clock_ms=lambda: NOW, **kwargs,
    )
    return launches, markets, store, worker


def add_candidate(launches, markets):
    launches.record_candidate(normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token", observed_at_ms=NOW - 1000,
    ))
    markets.record(SolanaMarketSnapshot(
        mint=MINT, selected_pair_address="pool", symbol="MEME",
        price_usd=0.001, selected_pool_liquidity_usd=5000,
        selected_pool_volume_h1_usd=1000, price_change_m5_pct=1.0,
        pair_count=1, retrieved_at_ms=NOW - 500, provider_observed_at_ms=NOW - 500,
        pair_created_at_ms=NOW - 1000, quality_flags=(),
    ))


def test_worker_persists_developer_linked_holder_supply(tmp_path):
    launches, markets, store, worker = setup(tmp_path)
    add_candidate(launches, markets)
    result = worker.run_once()
    assert (result.discovered, result.processed, result.stored, result.incomplete, result.failed) == (1, 1, 1, 0, 0)
    saved = store.latest(MINT)["report"]
    assert saved["coverage_complete"] is True
    assert saved["linked_supply_raw"] == 1000
    assert saved["dev_hold_pct"] == 100.0
    assert saved["provider"] is None  # Not production-certified; cannot become SafetyEvidence.
    assert saved["method"] == "exhaustive_holder_balances_with_root_reachable_funding_ancestry"


def test_worker_retries_when_root_attestation_is_unavailable(tmp_path):
    launches, markets, store, worker = setup(tmp_path)
    add_candidate(launches, markets)
    worker.root_provider.fetch_attestation = lambda *args: (_ for _ in ()).throw(RuntimeError("offline"))
    result = worker.run_once()
    assert result.failed == 1
    assert store.latest(MINT)["report"]["coverage_complete"] is False
    assert worker.status()["retry"] == 1


def test_worker_requires_explicit_provider_trust(tmp_path):
    path = tmp_path / "untrusted.sqlite3"
    launches = LaunchStore(path)
    store = DeveloperConcentrationStore(path)
    try:
        SolanaDeveloperEvidenceWorker(
            launches, HolderProvider(), FundingProvider(), RootProvider(), store,
            trusted_holder_providers=(), trusted_funding_providers=("helius-funding",),
            trusted_root_providers=("signed-roots",),
        )
        assert False, "expected untrusted provider rejection"
    except ValueError as exc:
        assert "explicitly trusted" in str(exc)


def test_store_is_append_only_and_deduplicates(tmp_path):
    launches, markets, store, worker = setup(tmp_path)
    add_candidate(launches, markets)
    worker.run_once()
    first = store.latest(MINT)
    assert first is not None
    assert store.record(worker._collect(MINT, NOW)) is False
    assert store.count(MINT) == 1
