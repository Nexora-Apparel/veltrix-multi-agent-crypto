"""Bounded one-shot Solana intelligence pipeline.

The pipeline runs discovery, enrichment, market refresh, risk evidence and
read-only consensus in a fixed order under one global fenced RunContext. It
never starts the PumpPortal stream supervisor. Paper processing remains
explicitly disabled until the paper broker supports transaction-scoped fencing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..orchestration import OrchestrationStore, RunContext, run_once as execute_job
from .consensus_worker import ConsensusBatchResult, ConsensusWorker
from .discovery import DiscoveryCycleResult, SolanaDiscoveryEngine
from .enrichment import CandidateEnrichmentWorker, EnrichmentBatchResult
from .market_refresh import MarketRefreshBatchResult, MarketRefreshWorker
from .risk_worker import RiskEvidenceBatchResult, SolanaRiskEvidenceWorker


@dataclass(frozen=True)
class SolanaPipelineStages:
    discovery: DiscoveryCycleResult
    enrichment: EnrichmentBatchResult
    market_refresh: MarketRefreshBatchResult
    risk: RiskEvidenceBatchResult
    consensus: ConsensusBatchResult


@dataclass(frozen=True)
class SolanaPipelineRun:
    job: dict[str, Any]
    stages: SolanaPipelineStages | None


class OneShotSolanaPipeline:
    """Execute one bounded pipeline attempt under one global fencing lease."""

    def __init__(
        self,
        orchestration: OrchestrationStore,
        discovery: SolanaDiscoveryEngine,
        enrichment: CandidateEnrichmentWorker,
        market_refresh: MarketRefreshWorker,
        risk: SolanaRiskEvidenceWorker,
        consensus: ConsensusWorker,
        *,
        paper_enabled: bool = False,
        max_runtime_seconds: float = 180.0,
        lease_ttl_ms: int = 240_000,
        max_items: int = 100,
    ) -> None:
        if paper_enabled:
            raise ValueError(
                "paper stage is blocked until PaperTradingWorker supports transaction-scoped fencing"
            )
        database_paths = {
            orchestration.database,
            discovery.store.database_path,
            enrichment.database_path,
            market_refresh.database_path,
            risk.database_path,
            consensus.database_path,
        }
        if len(database_paths) != 1:
            raise ValueError("all one-shot pipeline stages must share one persistent database")
        self.orchestration = orchestration
        self.discovery = discovery
        self.enrichment = enrichment
        self.market_refresh = market_refresh
        self.risk = risk
        self.consensus = consensus
        self.max_runtime_seconds = max_runtime_seconds
        self.lease_ttl_ms = lease_ttl_ms
        self.max_items = max_items

    def _execute_stages(self, context: RunContext, payload: dict[str, Any]) -> SolanaPipelineStages:
        discovery = self.discovery.poll_once(context=context, limit=25)
        enrichment = self.enrichment.run_once(limit=25, context=context)
        market_refresh = self.market_refresh.run_once(limit=25, context=context)
        risk = self.risk.run_once(limit=5, context=context)
        consensus = self.consensus.run_once(limit=10, context=context)
        return SolanaPipelineStages(
            discovery=discovery,
            enrichment=enrichment,
            market_refresh=market_refresh,
            risk=risk,
            consensus=consensus,
        )

    def run_once(self, *, idempotency_key: str) -> SolanaPipelineRun:
        """Execute no more than one pipeline attempt; retry only on a later call."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        stage_results: list[SolanaPipelineStages] = []

        def handler(context: RunContext, payload: dict[str, Any]) -> None:
            stage_results.append(self._execute_stages(context, payload))

        job = execute_job(
            self.orchestration,
            idempotency_key=idempotency_key,
            job_type="solana_intelligence_pipeline",
            payload={
                "stage_order": [
                    "discovery", "enrichment", "market_refresh", "risk", "consensus"
                ],
                "paper_enabled": False,
                "limits": {
                    "discovery": 25,
                    "enrichment": 25,
                    "market_refresh": 25,
                    "risk": 5,
                    "consensus": 10,
                },
            },
            handler=handler,
            max_runtime_seconds=self.max_runtime_seconds,
            max_items=self.max_items,
            lease_ttl_ms=self.lease_ttl_ms,
        )
        return SolanaPipelineRun(
            job=job,
            stages=stage_results[0] if stage_results else None,
        )


def build_one_shot_pipeline(
    *,
    database_path: Any | None = None,
    settings_obj: Any | None = None,
    clock_ms: Any | None = None,
    discovery_session: Any | None = None,
    market_provider: Any | None = None,
    risk_collector: Any | None = None,
    wallet_event_provider: Any | None = None,
    wallet_event_batch_provider: Any | None = None,
    paper_enabled: bool = False,
) -> OneShotSolanaPipeline:
    """Build production adapters without starting any long-lived supervisors.

    PumpPortal websocket supervision is intentionally disabled. Discovery uses
    one bounded secondary-source poll per invocation. The other stages reuse
    their existing provider factories and share the same durable database.
    """
    from .consensus_runtime import build_runtime as build_consensus_runtime
    from .intelligence_worker import build_runtime as build_intelligence_runtime
    from .risk_runtime import build_runtime as build_risk_runtime
    from .worker import build_engine

    if database_path is None:
        from .database import shared_database_path
        db_path = shared_database_path()
    else:
        from .database import shared_database_path
        db_path = shared_database_path(database_path)

    discovery = build_engine(
        database_path=db_path,
        http_session=discovery_session,
        include_primary_stream=False,
    )
    intelligence = build_intelligence_runtime(
        database_path=db_path,
        provider=market_provider,
        clock_ms=clock_ms,
    )
    risk_runtime = build_risk_runtime(
        database_path=db_path,
        settings_obj=settings_obj,
        collector=risk_collector,
        clock_ms=clock_ms,
    )
    consensus_runtime = build_consensus_runtime(
        database_path=db_path,
        worker_clock_ms=clock_ms,
        wallet_event_provider=wallet_event_provider,
        wallet_event_batch_provider=wallet_event_batch_provider,
    )

    orchestration = OrchestrationStore(str(db_path))
    orchestration.initialize()
    return OneShotSolanaPipeline(
        orchestration,
        discovery,
        intelligence.enrichment,
        intelligence.refresh,
        risk_runtime.worker,
        consensus_runtime.worker,
        paper_enabled=paper_enabled,
    )
