"""Bounded orchestration for the read-only Solana launch and market pipeline."""
from __future__ import annotations

from dataclasses import dataclass

from .enrichment import CandidateEnrichmentWorker, EnrichmentBatchResult
from .market_refresh import MarketRefreshBatchResult, MarketRefreshWorker


@dataclass(frozen=True)
class SolanaPipelineCycleResult:
    enrichment: EnrichmentBatchResult
    refresh: MarketRefreshBatchResult


class SolanaMemePipeline:
    """Run initial enrichment before scheduled refresh in each bounded cycle.

    The pipeline has no agent, broker, wallet, signer, or swap dependency.
    """

    def __init__(
        self,
        enrichment_worker: CandidateEnrichmentWorker,
        refresh_worker: MarketRefreshWorker,
    ) -> None:
        if enrichment_worker.database_path != refresh_worker.database_path:
            raise ValueError("enrichment and refresh workers must share one SQLite database")
        self.enrichment_worker = enrichment_worker
        self.refresh_worker = refresh_worker

    def run_once(self, *, limit: int = 25) -> SolanaPipelineCycleResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        enrichment = self.enrichment_worker.run_once(limit=limit)
        refresh = self.refresh_worker.run_once(limit=limit)
        return SolanaPipelineCycleResult(enrichment=enrichment, refresh=refresh)
