"""Supervised Phase 06 multi-agent consensus runtime.

Run with:
    python -m veltrix.solana_meme.consensus_runtime

This runtime produces evidence-backed analytical dispositions only. It cannot
create paper positions, sign transactions, or submit swaps.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .consensus import ConsensusEngine
from .consensus_store import ConsensusStore
from .consensus_worker import ConsensusBatchResult, ConsensusWorker
from .launch_stream import LaunchStore
from .market_store import MarketSnapshotStore
from .risk_store import SolanaRiskEvidenceStore
from .helius_wallet_events import HeliusWalletEventBatchProvider

LOGGER = logging.getLogger("veltrix.solana_consensus")


@dataclass(frozen=True)
class ConsensusRuntimeCycleResult:
    consensus: ConsensusBatchResult


class ConsensusRuntime:
    def __init__(
        self,
        worker: ConsensusWorker,
        *,
        poll_interval_seconds: float = 30.0,
        batch_size: int = 10,
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

    def run_once(self) -> ConsensusRuntimeCycleResult:
        return ConsensusRuntimeCycleResult(self.worker.run_once(limit=self.batch_size))

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            result = self.run_once().consensus
            LOGGER.info(
                "consensus cycle processed=%s recorded=%s unchanged=%s failed=%s deferred=%s",
                result.processed, result.recorded, result.unchanged,
                result.failed, result.deferred,
            )
            if stop_event.wait(self.poll_interval_seconds):
                break


def build_runtime(
    *,
    database_path: str | Path | None = None,
    poll_interval_seconds: float | None = None,
    refresh_interval_ms: int | None = None,
    batch_size: int | None = None,
    trusted_wallets: tuple[str, ...] | None = None,
    worker_clock_ms: Any | None = None,
    wallet_event_provider: Any | None = None,
    wallet_event_batch_provider: Any | None = None,
) -> ConsensusRuntime:
    db_path = database_path or os.getenv(
        "VELTRIX_SOLANA_DISCOVERY_DB",
        "data/solana_discovery.sqlite3",
    )
    try:
        poll = float(
            poll_interval_seconds
            if poll_interval_seconds is not None
            else os.getenv("VELTRIX_CONSENSUS_POLL_SECONDS", "30")
        )
        refresh_ms = int(
            refresh_interval_ms
            if refresh_interval_ms is not None
            else os.getenv("VELTRIX_CONSENSUS_REFRESH_INTERVAL_MS", "60000")
        )
        batch = int(
            batch_size
            if batch_size is not None
            else os.getenv("VELTRIX_CONSENSUS_BATCH_SIZE", "10")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("consensus runtime settings must be numeric") from exc
    if refresh_interval_ms is not None and (
        isinstance(refresh_interval_ms, bool) or not isinstance(refresh_interval_ms, int)
    ):
        raise ValueError("refresh_interval_ms must be an integer")
    if batch_size is not None and (
        isinstance(batch_size, bool) or not isinstance(batch_size, int)
    ):
        raise ValueError("batch_size must be an integer")

    wallets = trusted_wallets
    if wallets is None:
        wallet_setting = (
            os.getenv("VELTRIX_TRUSTED_SOLANA_WALLETS")
            or os.getenv("TRACKED_SMART_WALLETS", "")
        )
        wallets = tuple(item.strip() for item in wallet_setting.split(",") if item.strip())
    provider_ids = tuple(
        item.strip()
        for item in os.getenv("TRUSTED_WALLET_PROVIDERS", "").split(",")
        if item.strip()
    )
    engine = ConsensusEngine(trusted_wallets=wallets)
    launch_store = LaunchStore(db_path)
    market_store = MarketSnapshotStore(db_path)
    risk_store = SolanaRiskEvidenceStore(db_path)
    consensus_store = ConsensusStore(db_path)

    if wallet_event_provider is None and wallet_event_batch_provider is None:
        api_key = os.getenv("HELIUS_API_KEY", "")
        if api_key and wallets and "helius" in provider_ids:
            try:
                max_wallet_age = int(os.getenv("WALLET_EVIDENCE_MAX_AGE_MS", "300000"))
                wallet_event_batch_provider = HeliusWalletEventBatchProvider(
                    api_key=api_key,
                    trusted_wallets=wallets,
                    trusted_provider_ids=provider_ids,
                    market_store=market_store,
                    clock_ms=worker_clock_ms,
                    max_wallet_age_ms=max_wallet_age,
                )
                LOGGER.info("Helius wallet evidence feed enabled for %s explicitly allowlisted wallets", len(wallets))
            except (TypeError, ValueError) as exc:
                LOGGER.warning("Helius wallet evidence feed disabled by invalid configuration: %s", exc)
        else:
            LOGGER.info(
                "Helius wallet evidence feed disabled; requires HELIUS_API_KEY, "
                "TRUSTED_WALLET_PROVIDERS including helius, and an explicit wallet allowlist"
            )
    if wallet_event_provider is None:
        api_key = os.getenv("HELIUS_API_KEY", "").strip()
        trusted_provider_ids = {
            item.strip().lower()
            for item in os.getenv("TRUSTED_WALLET_PROVIDERS", "").split(",")
            if item.strip()
        }
        if api_key and wallets and "helius" in trusted_provider_ids:
            from ..providers.helius import HeliusWalletProvider
            from ..providers.intelligence import VerifiedIntelligenceAdapters
            from .helius_wallets import HeliusConsensusWalletEventSource

            try:
                max_wallets = int(os.getenv("VELTRIX_HELIUS_WALLET_SCAN_LIMIT", "10"))
                max_wallet_age_ms = int(os.getenv("WALLET_EVIDENCE_MAX_AGE_MS", "300000"))
            except ValueError as exc:
                raise ValueError("Helius wallet scan settings must be integers") from exc
            if not 2 <= max_wallets <= 25:
                raise ValueError("VELTRIX_HELIUS_WALLET_SCAN_LIMIT must be between 2 and 25")
            if len(wallets) > max_wallets:
                raise ValueError(
                    "trusted Solana wallet allowlist exceeds the configured Helius scan limit"
                )
            if max_wallet_age_ms < 1:
                raise ValueError("WALLET_EVIDENCE_MAX_AGE_MS must be positive")
            helius = HeliusWalletProvider(
                api_key=api_key,
                tracked_wallets=wallets,
                price_usd_resolver=lambda _mint: 0.0,
                timeout=10.0,
                limit=100,
                max_pages=2,
            )
            adapters = VerifiedIntelligenceAdapters(
                trusted_wallet_providers=("helius",),
                max_wallet_age_ms=max_wallet_age_ms,
            )
            wallet_event_provider = HeliusConsensusWalletEventSource(
                helius,
                adapters,
                market_store,
                clock_ms=worker_clock_ms or (lambda: int(time.time() * 1000)),
                max_age_ms=max_wallet_age_ms,
            )
            LOGGER.info(
                "Helius wallet intelligence enabled for %s explicitly trusted wallets",
                len(wallets),
            )
        else:
            LOGGER.info(
                "Whale agent will abstain: Helius key, trusted provider identity, "
                "and operator wallet allowlist are all required"
            )

    worker = ConsensusWorker(
        launch_store,
        market_store,
        risk_store,
        engine,
        consensus_store,
        refresh_interval_ms=refresh_ms,
        clock_ms=worker_clock_ms,
        wallet_event_provider=wallet_event_provider,
        wallet_event_batch_provider=wallet_event_batch_provider,
    )
    return ConsensusRuntime(
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
        "starting read-only Solana consensus runtime; batch_size=%s poll_interval=%s",
        runtime.batch_size,
        runtime.poll_interval_seconds,
    )
    runtime.run(stop_event)
    LOGGER.info("Solana consensus runtime stopped")


if __name__ == "__main__":
    main()
