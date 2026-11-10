"""Scheduled refresh of timestamped Solana market intelligence."""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ..database import connect_database, row_factory_for

from .launch_stream import LaunchStore
from .market_data import MarketDataBatch, MarketDataUnavailable, SolanaMarketData
from .market_store import MarketSnapshotStore


@dataclass(frozen=True)
class MarketRefreshBatchResult:
    discovered: int
    processed: int
    refreshed: int
    unchanged: int
    failed: int
    deferred: int


class MarketRefreshWorker:
    """Refresh discovered mints on a bounded cadence with durable retries."""

    def __init__(
        self,
        launch_store: LaunchStore,
        market_data: SolanaMarketData,
        market_store: MarketSnapshotStore,
        *,
        refresh_interval_ms: int = 60_000,
        max_snapshot_age_ms: int = 120_000,
        lease_ms: int = 120_000,
        retry_base_ms: int = 5_000,
        retry_max_ms: int = 300_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if launch_store.database_path != market_store.database_path:
            raise ValueError("launch and market stores must share one persistent SQLite database")
        if getattr(launch_store, "_uri", False):
            raise ValueError("market refresh requires a persistent SQLite database")
        for name, value in (
            ("refresh_interval_ms", refresh_interval_ms),
            ("max_snapshot_age_ms", max_snapshot_age_ms),
            ("lease_ms", lease_ms),
            ("retry_base_ms", retry_base_ms),
            ("retry_max_ms", retry_max_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if retry_max_ms < retry_base_ms:
            raise ValueError("retry_max_ms must be >= retry_base_ms")
        self.launch_store = launch_store
        self.market_data = market_data
        self.market_store = market_store
        self.database_path = launch_store.database_path
        self.refresh_interval_ms = refresh_interval_ms
        self.max_snapshot_age_ms = max_snapshot_age_ms
        self.lease_ms = lease_ms
        self.retry_base_ms = retry_base_ms
        self.retry_max_ms = retry_max_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        db = connect_database(self.database_path, timeout=10)
        db.row_factory = row_factory_for(self.database_path)
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_market_refresh_jobs (
                    mint TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('active', 'processing', 'retry')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    next_refresh_at_ms INTEGER NOT NULL,
                    lease_until_ms INTEGER,
                    last_attempt_at_ms INTEGER,
                    last_success_at_ms INTEGER,
                    last_error TEXT,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_market_refresh_due "
                "ON solana_market_refresh_jobs(status, next_refresh_at_ms, lease_until_ms)"
            )

    def _discover_jobs(self, now: int) -> int:
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_market_refresh_jobs
                    (mint, status, attempts, consecutive_failures,
                     next_refresh_at_ms, updated_at_ms)
                SELECT c.mint, 'active', 0, 0,
                       CASE WHEN MAX(s.retrieved_at_ms) + ? <= ?
                            THEN ? ELSE MAX(s.retrieved_at_ms) + ? END,
                       ?
                FROM solana_launch_candidates AS c
                JOIN solana_market_snapshots AS s ON s.mint = c.mint
                GROUP BY c.mint
            """, (
                self.refresh_interval_ms,
                now,
                now,
                self.refresh_interval_ms,
                now,
            ))
            return cursor.rowcount

    def _claim(self, now: int, limit: int) -> list[str]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE solana_market_refresh_jobs
                SET status = 'retry', lease_until_ms = NULL,
                    next_refresh_at_ms = ?, updated_at_ms = ?
                WHERE status = 'processing' AND lease_until_ms <= ?
            """, (now, now, now))
            rows = db.execute("""
                SELECT mint FROM solana_market_refresh_jobs
                WHERE status IN ('active', 'retry')
                  AND next_refresh_at_ms <= ?
                ORDER BY next_refresh_at_ms ASC, mint ASC
                LIMIT ?
            """, (now, limit)).fetchall()
            mints = [row["mint"] for row in rows]
            for mint in mints:
                db.execute("""
                    UPDATE solana_market_refresh_jobs
                    SET status = 'processing', attempts = attempts + 1,
                        last_attempt_at_ms = ?, lease_until_ms = ?, updated_at_ms = ?
                    WHERE mint = ?
                """, (now, now + self.lease_ms, now, mint))
            return mints

    def _succeed(self, mint: str, now: int) -> None:
        with self._connect() as db:
            db.execute("""
                UPDATE solana_market_refresh_jobs
                SET status = 'active', consecutive_failures = 0,
                    next_refresh_at_ms = ?, lease_until_ms = NULL,
                    last_success_at_ms = ?, last_error = NULL, updated_at_ms = ?
                WHERE mint = ?
            """, (now + self.refresh_interval_ms, now, now, mint))

    def _fail(self, mint: str, error: Exception, now: int) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT consecutive_failures FROM solana_market_refresh_jobs WHERE mint = ?",
                (mint,),
            ).fetchone()
            failures = (int(row["consecutive_failures"]) if row else 0) + 1
            delay = min(self.retry_base_ms * (2 ** min(failures - 1, 30)), self.retry_max_ms)
            db.execute("""
                UPDATE solana_market_refresh_jobs
                SET status = 'retry', consecutive_failures = ?,
                    next_refresh_at_ms = ?, lease_until_ms = NULL,
                    last_error = ?, updated_at_ms = ?
                WHERE mint = ?
            """, (
                failures,
                now + delay,
                f"{type(error).__name__}: {error}"[:500],
                now,
                mint,
            ))

    def run_once(self, *, limit: int = 25) -> MarketRefreshBatchResult:
        """Discover refresh targets and process one bounded batch via batch API."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 300:
            raise ValueError("limit must be between 1 and 300")
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("clock_ms must return a positive integer")
        discovered = self._discover_jobs(now)
        claimed = self._claim(now, limit)
        if not claimed:
            with self._connect() as db:
                deferred = int(db.execute("""
                    SELECT COUNT(*) FROM solana_market_refresh_jobs
                    WHERE status IN ('active', 'retry', 'processing')
                """).fetchone()[0])
            return MarketRefreshBatchResult(discovered, 0, 0, 0, 0, deferred)

        refreshed = unchanged = failed = 0
        try:
            batch = self.market_data.fetch_many(claimed, now_ms=self.clock_ms())
            if not isinstance(batch, MarketDataBatch):
                raise TypeError("market data adapter returned an invalid batch")
        except Exception as exc:
            for mint in claimed:
                self._fail(mint, exc, self.clock_ms())
            failed = len(claimed)
        else:
            for mint in claimed:
                snapshot = batch.snapshots.get(mint)
                if snapshot is None:
                    self._fail(
                        mint,
                        MarketDataUnavailable(
                            batch.unavailable.get(mint, "market snapshot unavailable")
                        ),
                        self.clock_ms(),
                    )
                    failed += 1
                    continue
                try:
                    if snapshot.mint != mint:
                        raise ValueError("market adapter returned a mismatched mint")
                    observed_at = self.clock_ms()
                    if (
                        isinstance(snapshot.retrieved_at_ms, bool)
                        or not isinstance(snapshot.retrieved_at_ms, int)
                        or snapshot.retrieved_at_ms > observed_at
                        or observed_at - snapshot.retrieved_at_ms > self.max_snapshot_age_ms
                    ):
                        raise ValueError("market snapshot retrieval timestamp is stale or invalid")
                    inserted = self.market_store.record(snapshot)
                    self._succeed(mint, self.clock_ms())
                    if inserted:
                        refreshed += 1
                    else:
                        unchanged += 1
                except Exception as exc:
                    self._fail(mint, exc, self.clock_ms())
                    failed += 1

        with self._connect() as db:
            deferred = int(db.execute("""
                SELECT COUNT(*) FROM solana_market_refresh_jobs
                WHERE status IN ('active', 'retry', 'processing')
            """).fetchone()[0])
        return MarketRefreshBatchResult(
            discovered=discovered,
            processed=len(claimed),
            refreshed=refreshed,
            unchanged=unchanged,
            failed=failed,
            deferred=deferred,
        )

    def status(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT status, COUNT(*) AS count
                FROM solana_market_refresh_jobs GROUP BY status
            """).fetchall()
            totals = db.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN last_success_at_ms IS NOT NULL THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN consecutive_failures > 0 THEN 1 ELSE 0 END) AS degraded
                FROM solana_market_refresh_jobs
            """).fetchone()
        result = {"active": 0, "processing": 0, "retry": 0}
        result.update({row["status"]: int(row["count"]) for row in rows})
        result["total"] = int(totals["total"] or 0)
        result["succeeded"] = int(totals["succeeded"] or 0)
        result["degraded"] = int(totals["degraded"] or 0)
        return result
