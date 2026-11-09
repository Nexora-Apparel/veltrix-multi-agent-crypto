"""Supervised Phase 07 paper-trading runtime.

Run with:
    python -m veltrix.solana_meme.paper_runtime

The runtime consumes only persisted Phase 06 consensus decisions and uses the
SQLite PaperBroker. It has no live exchange or wallet-execution capability.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ledger import PaperBroker
from .consensus_store import ConsensusStore
from .market_store import MarketSnapshotStore
from .paper_trading import PaperTradingBatchResult, PaperTradingPolicy, PaperTradingWorker
from .risk_store import SolanaRiskEvidenceStore

LOGGER = logging.getLogger("veltrix.paper_trading")


@dataclass(frozen=True)
class PaperRuntimeCycleResult:
    paper: PaperTradingBatchResult


class PaperTradingRuntime:
    def __init__(
        self,
        worker: PaperTradingWorker,
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
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 250:
            raise ValueError("batch_size must be between 1 and 250")
        self.worker = worker
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.batch_size = batch_size

    def run_once(self) -> PaperRuntimeCycleResult:
        return PaperRuntimeCycleResult(self.worker.run_once(limit=self.batch_size))

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                result = self.run_once().paper
                LOGGER.info(
                    "paper cycle scanned=%s opened=%s skipped=%s blocked=%s duplicates=%s exits=%s errors=%s",
                    result.scanned_decisions,
                    result.opened_positions,
                    result.skipped_decisions,
                    result.blocked_entries,
                    result.duplicate_decisions,
                    result.exit_fills,
                    result.errors,
                )
            except Exception:
                # A transient database/runtime error must not silently kill the
                # supervisor. The next bounded cycle retries from persisted state.
                LOGGER.exception("paper cycle failed safely; no live execution path exists")
            if stop_event.wait(self.poll_interval_seconds):
                break


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def build_runtime(
    *,
    database_path: str | Path | None = None,
    starting_balance: float | None = None,
    max_positions: int | None = None,
    max_position_usd: float | None = None,
    stop_loss_pct: float | None = None,
    poll_interval_seconds: float | None = None,
    batch_size: int | None = None,
    policy: PaperTradingPolicy | None = None,
    clock_ms: Any | None = None,
) -> PaperTradingRuntime:
    if os.getenv("LIVE_TRADING", "false").strip().lower() in {"1", "true", "yes", "on"}:
        raise RuntimeError("Phase 07 refuses to start while LIVE_TRADING is enabled")
    db_path = database_path or os.getenv(
        "VELTRIX_SOLANA_DISCOVERY_DB",
        os.getenv("DB_PATH", "data/solana_discovery.sqlite3"),
    )
    if database_path is None:
        discovery_setting = os.getenv("VELTRIX_SOLANA_DISCOVERY_DB", "").strip()
        legacy_ledger_setting = os.getenv("DB_PATH", "").strip()
        if discovery_setting and legacy_ledger_setting:
            discovery_resolved = str(Path(discovery_setting).expanduser().resolve())
            legacy_resolved = str(Path(legacy_ledger_setting).expanduser().resolve())
            if discovery_resolved != legacy_resolved:
                raise ValueError(
                    "VELTRIX_SOLANA_DISCOVERY_DB and DB_PATH must reference the same "
                    "SQLite file so Phase 07 reuses the existing paper ledger"
                )
    balance = starting_balance if starting_balance is not None else _env_float("PAPER_BALANCE_USD", 500.0)
    positions = max_positions if max_positions is not None else _env_int("MAX_OPEN_POSITIONS", 10)
    position_usd = max_position_usd if max_position_usd is not None else _env_float("MAX_POSITION_USD", 50.0)
    stop = stop_loss_pct if stop_loss_pct is not None else _env_float("STOP_LOSS_PCT", 0.06)
    poll = poll_interval_seconds if poll_interval_seconds is not None else _env_float("VELTRIX_PAPER_POLL_SECONDS", 15.0)
    batch = batch_size if batch_size is not None else _env_int("VELTRIX_PAPER_DECISION_BATCH_SIZE", 25)

    broker = PaperBroker(
        str(db_path),
        starting_balance=balance,
        max_positions=positions,
        max_position_usd=position_usd,
        stop_loss_pct=stop,
    )
    market_store = MarketSnapshotStore(db_path)
    risk_store = SolanaRiskEvidenceStore(db_path)
    consensus_store = ConsensusStore(db_path)
    configured_policy = policy or PaperTradingPolicy(
        position_usd=position_usd,
        fee_bps=_env_float("VELTRIX_PAPER_FEE_BPS", 100.0),
        slippage_bps=_env_float("VELTRIX_PAPER_SLIPPAGE_BPS", 50.0),
        max_decision_age_ms=_env_int("VELTRIX_PAPER_MAX_DECISION_AGE_MS", 120_000),
        max_market_age_ms=_env_int("VELTRIX_PAPER_MAX_MARKET_AGE_MS", 120_000),
        max_risk_age_ms=_env_int("VELTRIX_PAPER_MAX_RISK_AGE_MS", 300_000),
    )
    worker = PaperTradingWorker(
        consensus_store,
        market_store,
        risk_store,
        broker,
        policy=configured_policy,
        clock_ms=clock_ms,
    )
    return PaperTradingRuntime(
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
        "starting simulation-only paper runtime; batch_size=%s poll_interval=%s; live execution disabled",
        runtime.batch_size,
        runtime.poll_interval_seconds,
    )
    runtime.run(stop_event)
    LOGGER.info("paper trading runtime stopped")


if __name__ == "__main__":
    main()
