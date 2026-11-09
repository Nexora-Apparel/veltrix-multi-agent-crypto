"""Bounded orchestration for the read-only Solana launch and evidence pipeline."""
from __future__ import annotations

from dataclasses import dataclass

from .enrichment import CandidateEnrichmentWorker, EnrichmentBatchResult
from .market_refresh import MarketRefreshBatchResult, MarketRefreshWorker
from .risk_worker import RiskEvidenceBatchResult, SolanaRiskEvidenceWorker
from .developer_worker import DeveloperEvidenceBatchResult, SolanaDeveloperEvidenceWorker
from .bundle_worker import LaunchBundleBatchResult, SolanaLaunchBundleWorker
from .sellability_worker import SellabilityBatchResult, SolanaSellabilityWorker
from .sell_simulation_worker import SellSimulationBatchResult, SolanaSellSimulationWorker


@dataclass(frozen=True)
class SolanaPipelineCycleResult:
    enrichment: EnrichmentBatchResult
    risk: RiskEvidenceBatchResult | None
    developer: DeveloperEvidenceBatchResult | None
    bundle: LaunchBundleBatchResult | None
    refresh: MarketRefreshBatchResult
    sellability: SellabilityBatchResult | None = None
    sell_simulation: SellSimulationBatchResult | None = None


class SolanaMemePipeline:
    """Run enrichment, optional risk collection, then scheduled market refresh.

    The pipeline has no agent, broker, wallet, signer, or swap dependency.
    """

    def __init__(
        self,
        enrichment_worker: CandidateEnrichmentWorker,
        refresh_worker: MarketRefreshWorker,
        risk_worker: SolanaRiskEvidenceWorker | None = None,
        developer_worker: SolanaDeveloperEvidenceWorker | None = None,
        bundle_worker: SolanaLaunchBundleWorker | None = None,
        *,
        risk_batch_size: int = 5,
        developer_batch_size: int = 2,
        bundle_batch_size: int = 1,
        sellability_worker: SolanaSellabilityWorker | None = None,
        sellability_batch_size: int = 2,
        sell_simulation_worker: SolanaSellSimulationWorker | None = None,
        sell_simulation_batch_size: int = 1,
    ) -> None:
        paths = {enrichment_worker.database_path, refresh_worker.database_path}
        if risk_worker is not None:
            paths.add(risk_worker.database_path)
        if developer_worker is not None:
            paths.add(developer_worker.database_path)
        if bundle_worker is not None:
            paths.add(bundle_worker.database_path)
        if sellability_worker is not None:
            paths.add(sellability_worker.database_path)
        if sell_simulation_worker is not None:
            paths.add(sell_simulation_worker.database_path)
        if len(paths) != 1:
            raise ValueError("all Solana pipeline workers must share one SQLite database")
        if isinstance(risk_batch_size, bool) or not isinstance(risk_batch_size, int) or not 1 <= risk_batch_size <= 100:
            raise ValueError("risk_batch_size must be between 1 and 100")
        if isinstance(developer_batch_size, bool) or not isinstance(developer_batch_size, int) or not 1 <= developer_batch_size <= 25:
            raise ValueError("developer_batch_size must be between 1 and 25")
        self.enrichment_worker = enrichment_worker
        self.risk_worker = risk_worker
        if isinstance(bundle_batch_size, bool) or not isinstance(bundle_batch_size, int) or not 1 <= bundle_batch_size <= 10:
            raise ValueError("bundle_batch_size must be between 1 and 10")
        self.developer_worker = developer_worker
        self.bundle_worker = bundle_worker
        self.refresh_worker = refresh_worker
        self.risk_batch_size = risk_batch_size
        self.developer_batch_size = developer_batch_size
        self.bundle_batch_size = bundle_batch_size
        if isinstance(sellability_batch_size, bool) or not isinstance(sellability_batch_size, int) or not 1 <= sellability_batch_size <= 20:
            raise ValueError("sellability_batch_size must be between 1 and 20")
        self.sellability_worker = sellability_worker
        self.sellability_batch_size = sellability_batch_size
        if isinstance(sell_simulation_batch_size, bool) or not isinstance(sell_simulation_batch_size, int) or not 1 <= sell_simulation_batch_size <= 10:
            raise ValueError("sell_simulation_batch_size must be between 1 and 10")
        self.sell_simulation_worker = sell_simulation_worker
        self.sell_simulation_batch_size = sell_simulation_batch_size

    def run_once(self, *, limit: int = 25) -> SolanaPipelineCycleResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        enrichment = self.enrichment_worker.run_once(limit=limit)
        risk = (
            self.risk_worker.run_once(limit=min(limit, self.risk_batch_size))
            if self.risk_worker is not None else None
        )
        developer = (
            self.developer_worker.run_once(limit=min(limit, self.developer_batch_size))
            if self.developer_worker is not None else None
        )
        bundle = (
            self.bundle_worker.run_once(limit=min(limit, self.bundle_batch_size))
            if self.bundle_worker is not None else None
        )
        sellability = (
            self.sellability_worker.run_once(limit=min(limit, self.sellability_batch_size))
            if self.sellability_worker is not None else None
        )
        sell_simulation = (
            self.sell_simulation_worker.run_once(limit=min(limit, self.sell_simulation_batch_size))
            if self.sell_simulation_worker is not None else None
        )
        refresh = self.refresh_worker.run_once(limit=limit)
        return SolanaPipelineCycleResult(
            enrichment=enrichment, risk=risk, developer=developer,
            bundle=bundle, refresh=refresh, sellability=sellability,
            sell_simulation=sell_simulation,
        )
