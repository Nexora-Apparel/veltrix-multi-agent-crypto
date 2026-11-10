"""Continuous, recoverable supervisor for the fenced one-shot Solana pipeline.

This process does not bypass orchestration leases: each bounded cycle delegates
to OneShotSolanaPipeline, whose durable idempotency key prevents duplicate work.
Health is written separately so failed cycles remain observable.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from ..database import connect_database, row_factory_for
from .database import shared_database_path
from .pipeline import build_one_shot_pipeline

LOGGER = logging.getLogger("veltrix.solana_pipeline_runtime")


class SolanaPipelineRuntime:
    def __init__(
        self,
        pipeline: Any,
        database_path: str,
        *,
        poll_interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(poll_interval_seconds, bool) or not isinstance(poll_interval_seconds, (int, float)) or not 15 <= poll_interval_seconds <= 3600:
            raise ValueError("poll_interval_seconds must be between 15 and 3600")
        self.pipeline = pipeline
        self.database_path = str(database_path)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.clock = clock
        self._initialize_health()

    def _connect(self):
        db = connect_database(self.database_path, timeout=10)
        db.row_factory = row_factory_for(self.database_path)
        return db

    def _initialize_health(self) -> None:
        db = self._connect()
        try:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_pipeline_health (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    status TEXT NOT NULL,
                    last_started_at_ms INTEGER,
                    last_finished_at_ms INTEGER,
                    last_success_at_ms INTEGER,
                    last_duration_ms INTEGER,
                    last_job_id TEXT,
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            db.execute("""
                INSERT OR IGNORE INTO solana_pipeline_health
                    (id,status,consecutive_failures,updated_at_ms)
                VALUES (1,'starting',0,?)
            """, (int(self.clock() * 1000),))
            db.commit()
        finally:
            db.close()

    def _health_update(self, *, status: str, started: int, finished: int,
                       job_id: str | None = None, error: str | None = None) -> None:
        db = self._connect()
        try:
            row = db.execute(
                "SELECT consecutive_failures FROM solana_pipeline_health WHERE id=1"
            ).fetchone()
            failures = int(row["consecutive_failures"] if row else 0)
            if status == "healthy":
                failures = 0
            elif status == "degraded":
                failures += 1
            db.execute("""
                UPDATE solana_pipeline_health SET status=?,
                    last_started_at_ms=?,last_finished_at_ms=?,
                    last_success_at_ms=CASE WHEN ?='healthy' THEN ? ELSE last_success_at_ms END,
                    last_duration_ms=?,last_job_id=?,last_error=?,
                    consecutive_failures=?,updated_at_ms=? WHERE id=1
            """, (status, started, finished, status, finished,
                  max(0, finished-started), job_id, error[:500] if error else None,
                  failures, finished))
            db.commit()
        finally:
            db.close()

    def run_once(self) -> Any:
        started = int(self.clock() * 1000)
        self._health_update(status="running", started=started, finished=started)
        try:
            # A stable UTC bucket means restarts/retries in the same slot are
            # idempotent; orchestration persists the result across processes.
            bucket = int(self.clock() // self.poll_interval_seconds)
            slot = datetime.fromtimestamp(
                bucket * self.poll_interval_seconds, tz=timezone.utc
            ).strftime("%Y%m%dT%H%M%SZ")
            result = self.pipeline.run_once(idempotency_key=f"solana-pipeline-{slot}")
            finished = int(self.clock() * 1000)
            status = "healthy" if result.job.get("status") == "SUCCEEDED" else "degraded"
            error = None if status == "healthy" else str(result.job.get("error") or result.job.get("status"))
            self._health_update(status=status, started=started, finished=finished,
                                job_id=str(result.job.get("id") or "") or None, error=error)
            return result
        except Exception as exc:
            finished = int(self.clock() * 1000)
            try:
                self._health_update(status="degraded", started=started, finished=finished,
                                    error=f"{type(exc).__name__}: {exc}")
            except Exception:
                LOGGER.exception("could not persist pipeline health after cycle failure")
            raise

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                result = self.run_once()
                LOGGER.info("pipeline cycle status=%s executed=%s",
                            result.job.get("status"), result.job.get("executed"))
            except Exception:
                LOGGER.exception("pipeline cycle failed; supervisor will retry next interval")
            if stop_event.wait(self.poll_interval_seconds):
                break


def build_runtime(*, database_path: str | None = None,
                  poll_interval_seconds: float | None = None) -> SolanaPipelineRuntime:
    db_path = shared_database_path(database_path)
    raw = poll_interval_seconds if poll_interval_seconds is not None else os.getenv("VELTRIX_PIPELINE_POLL_SECONDS", "60")
    try:
        interval = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("VELTRIX_PIPELINE_POLL_SECONDS must be numeric") from exc
    live_enabled = os.getenv("LIVE_TRADING", "false").strip().lower() in {"1", "true", "yes", "on"}
    if live_enabled:
        raise RuntimeError("Phase 09 live execution is disabled; LIVE_TRADING must remain false")
    paper_enabled = os.getenv("PAPER_TRADING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    sol_paper_enabled = os.getenv("SOL_PAPER_TRADING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if paper_enabled and sol_paper_enabled:
        raise RuntimeError("enable only one paper ledger: USD or native SOL")
    pipeline = build_one_shot_pipeline(
        database_path=db_path, paper_enabled=paper_enabled,
        sol_paper_enabled=sol_paper_enabled,
    )
    return SolanaPipelineRuntime(pipeline, str(db_path), poll_interval_seconds=interval)


def main() -> None:
    logging.basicConfig(level=os.getenv("VELTRIX_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = threading.Event()
    def shutdown(signum: int, _frame: Any) -> None:
        LOGGER.info("received shutdown signal %s", signum)
        stop.set()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    runtime = build_runtime()
    LOGGER.info(
        "starting fenced Solana pipeline supervisor interval=%ss usd_paper=%s sol_paper=%s live_trading=false",
        runtime.poll_interval_seconds,
        os.getenv("PAPER_TRADING_ENABLED", "false").lower() in {"1","true","yes","on"},
        os.getenv("SOL_PAPER_TRADING_ENABLED", "false").lower() in {"1","true","yes","on"},
    )
    runtime.run(stop)


if __name__ == "__main__":
    main()
