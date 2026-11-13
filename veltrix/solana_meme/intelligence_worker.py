"""Supervised Phase 04 token-intelligence worker runtime.

Run with:
    python -m veltrix.solana_meme.intelligence_worker

The runtime reads discovered mints, fetches batched DEX Screener market data,
stores append-only snapshots, and refreshes them on a cadence. It never scores,
approves, paper-trades, or executes tokens.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..providers.dexscreener import DexScreenerProvider
from ..foundation import validate_runtime_controls
from .database import shared_database_path
from .enrichment import CandidateEnrichmentWorker, EnrichmentBatchResult
from .launch_stream import LaunchStore
from .market_data import SolanaMarketData
from .market_refresh import MarketRefreshBatchResult, MarketRefreshWorker
from .market_store import MarketSnapshotStore

LOGGER = logging.getLogger("veltrix.token_intelligence")


@dataclass(frozen=True)
class TokenIntelligenceCycleResult:
    enrichment: EnrichmentBatchResult
    refresh: MarketRefreshBatchResult


class TokenIntelligenceRuntime:
    def __init__(
        self,
        enrichment: CandidateEnrichmentWorker,
        refresh: MarketRefreshWorker,
        *,
        poll_interval_seconds: float = 15.0,
        batch_size: int = 25,
    ) -> None:
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or not 5 <= poll_interval_seconds <= 3600
        ):
            raise ValueError("poll_interval_seconds must be between 5 and 3600")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 30:
            raise ValueError("batch_size must be between 1 and 30")
        self.enrichment = enrichment
        self.refresh = refresh
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.batch_size = batch_size

    def run_once(self) -> TokenIntelligenceCycleResult:
        enriched = self.enrichment.run_once(limit=self.batch_size)
        refreshed = self.refresh.run_once(limit=self.batch_size)
        return TokenIntelligenceCycleResult(enriched, refreshed)

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            result = self.run_once()
            LOGGER.debug(
                "token-intelligence cycle: enriched=%s enrichment_failed=%s "
                "refreshed=%s refresh_failed=%s",
                result.enrichment.enriched,
                result.enrichment.failed,
                result.refresh.refreshed,
                result.refresh.failed,
            )
            if stop_event.wait(self.poll_interval_seconds):
                break


def build_runtime(
    *,
    database_path: str | Path | None = None,
    poll_interval_seconds: float | None = None,
    refresh_interval_ms: int | None = None,
    batch_size: int | None = None,
    provider: Any | None = None,
    clock_ms: Any | None = None,
) -> TokenIntelligenceRuntime:
    validate_runtime_controls()
    db_path = shared_database_path(database_path)
    if refresh_interval_ms is not None and (
        isinstance(refresh_interval_ms, bool) or not isinstance(refresh_interval_ms, int)
    ):
        raise ValueError("refresh_interval_ms must be an integer")
    if batch_size is not None and (
        isinstance(batch_size, bool) or not isinstance(batch_size, int)
    ):
        raise ValueError("batch_size must be an integer")

    try:
        poll_interval = float(
            poll_interval_seconds
            if poll_interval_seconds is not None
            else os.getenv("VELTRIX_MARKET_INTELLIGENCE_POLL_SECONDS", "15")
        )
        refresh_ms = int(
            refresh_interval_ms
            if refresh_interval_ms is not None
            else os.getenv("VELTRIX_MARKET_REFRESH_INTERVAL_MS", "60000")
        )
        batch_limit = int(
            batch_size
            if batch_size is not None
            else os.getenv("VELTRIX_MARKET_BATCH_SIZE", "25")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("token-intelligence worker settings must be numeric") from exc

    launch_store = LaunchStore(db_path)
    market_store = MarketSnapshotStore(db_path)
    market_data = SolanaMarketData(
        provider or DexScreenerProvider(),
        clock_ms=clock_ms,
    )
    enrichment = CandidateEnrichmentWorker(
        launch_store,
        market_data,
        market_store,
        clock_ms=clock_ms,
    )
    refresh = MarketRefreshWorker(
        launch_store,
        market_data,
        market_store,
        refresh_interval_ms=refresh_ms,
        clock_ms=clock_ms,
    )
    return TokenIntelligenceRuntime(
        enrichment,
        refresh,
        poll_interval_seconds=poll_interval,
        batch_size=batch_limit,
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
        "starting read-only token intelligence worker; batch_size=%s poll_interval=%s",
        runtime.batch_size,
        runtime.poll_interval_seconds,
    )
    runtime.run(stop_event)
    LOGGER.info("token intelligence worker stopped")


if __name__ == "__main__":
    main()
