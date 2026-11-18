"""Bounded one-shot Solana intelligence pipeline.

The pipeline runs discovery, enrichment, market refresh, risk evidence and
read-only consensus in a fixed order under one global fenced RunContext. It
never starts the PumpPortal stream supervisor. Paper processing remains
explicitly disabled until the paper broker supports transaction-scoped fencing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Any

from ..orchestration import OrchestrationStore, RunContext, run_once as execute_job
from .consensus_worker import ConsensusBatchResult, ConsensusWorker
from .discovery import DiscoveryCycleResult, SolanaDiscoveryEngine
from .enrichment import CandidateEnrichmentWorker, EnrichmentBatchResult
from .market_refresh import MarketRefreshBatchResult, MarketRefreshWorker
from .risk_worker import RiskEvidenceBatchResult, SolanaRiskEvidenceWorker
from .paper_trading import PaperTradingBatchResult, PaperTradingWorker


@dataclass(frozen=True)
class SolanaPipelineStages:
    discovery: DiscoveryCycleResult
    enrichment: EnrichmentBatchResult
    market_refresh: MarketRefreshBatchResult
    risk: RiskEvidenceBatchResult
    consensus: ConsensusBatchResult
    paper: PaperTradingBatchResult | None = None


@dataclass(frozen=True)
class SolanaPipelineRun:
    job: dict[str, Any]
    stages: SolanaPipelineStages | None
    stage_progress: tuple[dict[str, Any], ...] = ()


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
        paper_worker: PaperTradingWorker | None = None,
        paper_enabled: bool = False,
        max_runtime_seconds: float = 240.0,
        lease_ttl_ms: int = 300_000,
        max_items: int = 150,
        stage_limits: dict[str, int] | None = None,
    ) -> None:
        if paper_enabled and paper_worker is None:
            raise ValueError("paper_enabled requires a transaction-fenced PaperTradingWorker")
        database_paths = {
            orchestration.database,
            discovery.store.database_path,
            enrichment.database_path,
            market_refresh.database_path,
            risk.database_path,
            consensus.database_path,
        }
        if paper_worker is not None:
            database_paths.add(paper_worker.broker.db_path)
        if len(database_paths) != 1:
            raise ValueError("all one-shot pipeline stages must share one persistent database")
        self.orchestration = orchestration
        self.discovery = discovery
        self.enrichment = enrichment
        self.market_refresh = market_refresh
        self.risk = risk
        self.consensus = consensus
        self.paper_worker = paper_worker
        self.paper_enabled = paper_enabled
        self.max_runtime_seconds = max_runtime_seconds
        self.lease_ttl_ms = lease_ttl_ms
        self.max_items = max_items
        defaults = {
            "discovery": 25,
            "enrichment": 10,
            "market_refresh": 10,
            "risk": 5,
            "consensus": 10,
            "paper": 5,
        }
        if stage_limits is not None:
            if not isinstance(stage_limits, dict) or set(stage_limits) - set(defaults):
                raise ValueError("stage_limits contains unsupported stage names")
            for name, value in stage_limits.items():
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 300:
                    raise ValueError(f"stage limit for {name} must be between 1 and 300")
            defaults.update(stage_limits)
        self.stage_limits = defaults

    @staticmethod
    def _run_stage(
        name: str,
        operation: Any,
        progress: list[dict[str, Any]],
    ) -> Any:
        started_ns = time.monotonic_ns()
        try:
            result = operation()
        except Exception as exc:
            # Exception messages may contain provider payloads or token addresses.
            progress.append({
                "stage": name,
                "status": "FAILED",
                "error_type": type(exc).__name__,
                "elapsed_ms": max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
            })
            raise
        progress.append({
            "stage": name,
            "status": "COMPLETED",
            "elapsed_ms": max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
            "result": asdict(result),
        })
        return result

    def _execute_stages(
        self,
        context: RunContext,
        payload: dict[str, Any],
        progress: list[dict[str, Any]],
    ) -> SolanaPipelineStages:
        # Preserve bounded execution and global lease fencing between every stage.
        context.checkpoint(items=0)
        discovery = self._run_stage(
            "discovery",
            lambda: self.discovery.poll_once(context=context, limit=self.stage_limits["discovery"]),
            progress,
        )
        context.checkpoint(items=0)
        enrichment = self._run_stage(
            "enrichment",
            lambda: self.enrichment.run_once(limit=self.stage_limits["enrichment"], context=context),
            progress,
        )
        context.checkpoint(items=0)
        market_refresh = self._run_stage(
            "market_refresh",
            lambda: self.market_refresh.run_once(limit=self.stage_limits["market_refresh"], context=context),
            progress,
        )
        context.checkpoint(items=0)
        risk = self._run_stage(
            "risk",
            lambda: self.risk.run_once(limit=self.stage_limits["risk"], context=context),
            progress,
        )
        context.checkpoint(items=0)
        consensus = self._run_stage(
            "consensus",
            lambda: self.consensus.run_once(limit=self.stage_limits["consensus"], context=context),
            progress,
        )
        context.checkpoint(items=0)
        paper = None
        if self.paper_enabled and self.paper_worker is not None:
            paper = self._run_stage(
                "paper",
                lambda: self.paper_worker.run_once(limit=self.stage_limits["paper"], context=context),
                progress,
            )
        context.checkpoint(items=0)
        return SolanaPipelineStages(
            discovery=discovery,
            enrichment=enrichment,
            market_refresh=market_refresh,
            risk=risk,
            consensus=consensus,
            paper=paper,
        )

    def run_once(self, *, idempotency_key: str) -> SolanaPipelineRun:
        """Execute no more than one pipeline attempt; retry only on a later call."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        stage_results: list[SolanaPipelineStages] = []
        stage_progress: list[dict[str, Any]] = []

        def handler(context: RunContext, payload: dict[str, Any]) -> None:
            stage_results.append(self._execute_stages(context, payload, stage_progress))

        job = execute_job(
            self.orchestration,
            idempotency_key=idempotency_key,
            job_type="solana_intelligence_pipeline",
            payload={
                "stage_order": [
                    "discovery", "enrichment", "market_refresh", "risk", "consensus",
                    *(["paper"] if self.paper_enabled else []),
                ],
                "paper_enabled": self.paper_enabled,
                "limits": {
                    name: self.stage_limits[name]
                    for name in ("discovery", "enrichment", "market_refresh", "risk", "consensus")
                } | ({"paper": self.stage_limits["paper"]} if self.paper_enabled else {}),
            },
            handler=handler,
            max_runtime_seconds=self.max_runtime_seconds,
            max_items=self.max_items,
            lease_ttl_ms=self.lease_ttl_ms,
        )
        return SolanaPipelineRun(
            job=job,
            stages=stage_results[0] if stage_results else None,
            stage_progress=tuple(stage_progress),
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
    paper_worker = None
    if paper_enabled:
        from .paper_runtime import build_runtime as build_paper_runtime
        paper_worker = build_paper_runtime(
            database_path=db_path,
            clock_ms=clock_ms,
        ).worker

    return OneShotSolanaPipeline(
        orchestration,
        discovery,
        intelligence.enrichment,
        intelligence.refresh,
        risk_runtime.worker,
        consensus_runtime.worker,
        paper_worker=paper_worker,
        paper_enabled=paper_enabled,
    )
