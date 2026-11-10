"""Retry-safe initial enrichment from discovered mints to market snapshots."""
from __future__ import annotations

import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ..database import connect_database, row_factory_for

from .launch_stream import LaunchStore
from .market_data import MarketDataBatch, MarketDataUnavailable, SolanaMarketData
from .market_store import MarketSnapshotStore


@dataclass(frozen=True)
class EnrichmentBatchResult:
    discovered: int
    processed: int
    enriched: int
    failed: int
    deferred: int


class CandidateEnrichmentWorker:
    """Fetch one initial market snapshot per discovered mint; never emits trade signals.

    The launch and market stores must share one persistent database so the
    candidate queue and evidence ledger remain in one durable transaction domain.
    """

    def __init__(
        self,
        launch_store: LaunchStore,
        market_data: SolanaMarketData,
        market_store: MarketSnapshotStore,
        *,
        max_attempts: int = 5,
        lease_ms: int = 120_000,
        retry_base_ms: int = 5_000,
        retry_max_ms: int = 300_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if launch_store.database_path != market_store.database_path:
            raise ValueError("launch and market stores must share one persistent database")
        if getattr(launch_store, "_uri", False):
            raise ValueError("enrichment requires a persistent SQLite database")
        for name, value in (
            ("max_attempts", max_attempts),
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
        self.max_attempts = max_attempts
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
                CREATE TABLE IF NOT EXISTS solana_enrichment_jobs (
                    mint TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending', 'processing', 'retry', 'completed', 'dead')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at_ms INTEGER NOT NULL,
                    lease_until_ms INTEGER,
                    last_error TEXT,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_enrichment_due "
                "ON solana_enrichment_jobs(status, next_attempt_at_ms, lease_until_ms)"
            )

    def _discover_jobs(self, now: int) -> int:
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_enrichment_jobs
                    (mint, status, attempts, next_attempt_at_ms, updated_at_ms)
                SELECT mint, 'pending', 0, ?, ?
                FROM solana_launch_candidates
            """, (now, now))
            return cursor.rowcount

    def _claim(self, now: int, limit: int) -> list[str]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE solana_enrichment_jobs
                SET status = 'dead', lease_until_ms = NULL,
                    last_error = COALESCE(last_error, 'processing lease expired at attempt limit'),
                    updated_at_ms = ?
                WHERE status = 'processing' AND lease_until_ms <= ?
                  AND attempts >= ?
            """, (now, now, self.max_attempts))
            db.execute("""
                UPDATE solana_enrichment_jobs
                SET status = 'retry', lease_until_ms = NULL,
                    next_attempt_at_ms = ?, updated_at_ms = ?
                WHERE status = 'processing' AND lease_until_ms <= ?
                  AND attempts < ?
            """, (now, now, now, self.max_attempts))
            rows = db.execute("""
                SELECT mint FROM solana_enrichment_jobs
                WHERE status IN ('pending', 'retry')
                  AND next_attempt_at_ms <= ?
                  AND attempts < ?
                ORDER BY next_attempt_at_ms ASC, mint ASC
                LIMIT ?
            """, (now, self.max_attempts, limit)).fetchall()
            mints = [row["mint"] for row in rows]
            for mint in mints:
                db.execute("""
                    UPDATE solana_enrichment_jobs
                    SET status = 'processing', attempts = attempts + 1,
                        lease_until_ms = ?, updated_at_ms = ?
                    WHERE mint = ?
                """, (now + self.lease_ms, now, mint))
            return mints

    def _complete(self, mint: str, now: int) -> None:
        with self._connect() as db:
            db.execute("""
                UPDATE solana_enrichment_jobs
                SET status = 'completed', lease_until_ms = NULL,
                    last_error = NULL, updated_at_ms = ?
                WHERE mint = ?
            """, (now, mint))

    def _fail(self, mint: str, error: Exception, now: int) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT attempts FROM solana_enrichment_jobs WHERE mint = ?",
                (mint,),
            ).fetchone()
            attempts = int(row["attempts"]) if row else self.max_attempts
            delay = min(self.retry_base_ms * (2 ** min(max(0, attempts - 1), 30)), self.retry_max_ms)
            status = "dead" if attempts >= self.max_attempts else "retry"
            db.execute("""
                UPDATE solana_enrichment_jobs
                SET status = ?, next_attempt_at_ms = ?, lease_until_ms = NULL,
                    last_error = ?, updated_at_ms = ?
                WHERE mint = ?
            """, (
                status,
                now + delay,
                f"{type(error).__name__}: {error}"[:500],
                now,
                mint,
            ))

    def run_once(self, *, limit: int = 25) -> EnrichmentBatchResult:
        """Discover and process one bounded batch using one provider batch request."""
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
                    SELECT COUNT(*) FROM solana_enrichment_jobs
                    WHERE status IN ('pending', 'retry', 'processing')
                """).fetchone()[0])
            return EnrichmentBatchResult(discovered, 0, 0, 0, deferred)

        enriched = failed = 0
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
                    self.market_store.record(snapshot)
                    self._complete(mint, self.clock_ms())
                    enriched += 1
                except Exception as exc:
                    self._fail(mint, exc, self.clock_ms())
                    failed += 1

        with self._connect() as db:
            deferred = int(db.execute("""
                SELECT COUNT(*) FROM solana_enrichment_jobs
                WHERE status IN ('pending', 'retry', 'processing')
            """).fetchone()[0])
        return EnrichmentBatchResult(
            discovered=discovered,
            processed=len(claimed),
            enriched=enriched,
            failed=failed,
            deferred=deferred,
        )

    def status(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT status, COUNT(*) AS count
                FROM solana_enrichment_jobs GROUP BY status
            """).fetchall()
        result = {key: 0 for key in ("pending", "processing", "retry", "completed", "dead")}
        result.update({row["status"]: int(row["count"]) for row in rows})
        return result
