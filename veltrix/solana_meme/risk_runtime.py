"""Supervised Phase 05 security-evidence runtime.

Run with:
    python -m veltrix.solana_meme.risk_runtime

The worker reads candidates already enriched in Phase 04, collects finalized
mint and holder evidence plus optional GoPlus observations, and stores reports.
It never authorizes a trade or returns a SAFE verdict.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings as load_settings
from ..providers.factory import build_solana_risk_evidence_collector
from .launch_stream import LaunchStore
from .market_store import MarketSnapshotStore
from .risk_store import SolanaRiskEvidenceStore
from .risk_worker import RiskEvidenceBatchResult, SolanaRiskEvidenceWorker

LOGGER = logging.getLogger("veltrix.solana_risk")


@dataclass(frozen=True)
class RiskRuntimeCycleResult:
    risk: RiskEvidenceBatchResult


class SolanaRiskRuntime:
    def __init__(
        self,
        worker: SolanaRiskEvidenceWorker,
        *,
        poll_interval_seconds: float = 30.0,
        batch_size: int = 5,
    ) -> None:
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not 5 <= poll_interval_seconds <= 3600
        ):
            raise ValueError("poll_interval_seconds must be between 5 and 3600")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 25:
            raise ValueError("batch_size must be between 1 and 25")
        self.worker = worker
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.batch_size = batch_size

    def run_once(self) -> RiskRuntimeCycleResult:
        return RiskRuntimeCycleResult(self.worker.run_once(limit=self.batch_size))

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            result = self.run_once().risk
            LOGGER.info(
                "risk evidence cycle processed=%s recorded=%s failed=%s deferred=%s",
                result.processed, result.recorded, result.failed, result.deferred,
            )
            if stop_event.wait(self.poll_interval_seconds):
                break


def build_runtime(
    *,
    database_path: str | Path | None = None,
    poll_interval_seconds: float | None = None,
    refresh_interval_ms: int | None = None,
    batch_size: int | None = None,
    settings_obj: Any | None = None,
    collector: Any | None = None,
    clock_ms: Any | None = None,
) -> SolanaRiskRuntime:
    config = settings_obj or load_settings()
    db_path = database_path or os.getenv(
        "VELTRIX_SOLANA_DISCOVERY_DB",
        "data/solana_discovery.sqlite3",
    )
    try:
        poll = float(
            poll_interval_seconds
            if poll_interval_seconds is not None
            else os.getenv("VELTRIX_RISK_POLL_SECONDS", "30")
        )
        refresh_ms = int(
            refresh_interval_ms
            if refresh_interval_ms is not None
            else os.getenv("VELTRIX_RISK_REFRESH_INTERVAL_MS", "300000")
        )
        batch = int(
            batch_size
            if batch_size is not None
            else os.getenv("VELTRIX_RISK_BATCH_SIZE", "5")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("risk runtime settings must be numeric") from exc
    if refresh_interval_ms is not None and (
        isinstance(refresh_interval_ms, bool) or not isinstance(refresh_interval_ms, int)
    ):
        raise ValueError("refresh_interval_ms must be an integer")
    if batch_size is not None and (
        isinstance(batch_size, bool) or not isinstance(batch_size, int)
    ):
        raise ValueError("batch_size must be an integer")

    risk_collector = collector or build_solana_risk_evidence_collector(
        config, clock_ms=clock_ms
    )
    if risk_collector is None:
        raise ValueError("Configure SOLANA_RPC_URL or HELIUS_API_KEY before starting Phase 05")
    launch_store = LaunchStore(db_path)
    market_store = MarketSnapshotStore(db_path)
    risk_store = SolanaRiskEvidenceStore(db_path)
    worker = SolanaRiskEvidenceWorker(
        launch_store,
        market_store,
        risk_collector,
        risk_store,
        refresh_interval_ms=refresh_ms,
        clock_ms=clock_ms,
    )
    return SolanaRiskRuntime(
        worker,
        poll_interval_seconds=poll,
        batch_size=batch,
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("VELTRIX_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = threading.Event()

    def request_shutdown(signum: int, _frame: Any) -> None:
        LOGGER.info("received shutdown signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)

    runtime = build_runtime()
    LOGGER.info(
        "starting read-only Solana risk evidence worker; batch_size=%s poll_interval=%s",
        runtime.batch_size,
        runtime.poll_interval_seconds,
    )
    runtime.run(stop_event)
    LOGGER.info("Solana risk evidence worker stopped")


if __name__ == "__main__":
    main()
