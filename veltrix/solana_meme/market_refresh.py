"""Scheduled refresh of timestamped Solana market intelligence."""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ..database import connect_database, row_factory_for
from ..orchestration import LeaseBusy, RunContext

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
            raise ValueError("launch and market stores must share one persistent database")
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
                    claimed_by_owner_id TEXT,
                    claim_fencing_token INTEGER,
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
            columns = {row["name"] for row in db.execute(
                "PRAGMA table_info(solana_market_refresh_jobs)"
            ).fetchall()}
            if "claimed_by_owner_id" not in columns:
                db.execute("ALTER TABLE solana_market_refresh_jobs ADD COLUMN claimed_by_owner_id TEXT")
            if "claim_fencing_token" not in columns:
                db.execute("ALTER TABLE solana_market_refresh_jobs ADD COLUMN claim_fencing_token INTEGER")

    @staticmethod
    def _assert_claim(db: Any, mint: str, now: int, context: RunContext) -> None:
        if not context.owner_id:
            raise LeaseBusy("run context has no lease owner identity")
        row = db.execute(
            """SELECT 1 FROM solana_market_refresh_jobs
               WHERE mint=? AND status='processing' AND claimed_by_owner_id=?
                 AND claim_fencing_token=? AND lease_until_ms>?""",
            (mint, context.owner_id, context.fencing_token, now),
        ).fetchone()
        if row is None:
            raise LeaseBusy("market refresh row claim expired or was replaced")

    def _discover_jobs(self, now: int, context: RunContext | None = None) -> int:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
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
                self.refresh_interval_ms, now, now, self.refresh_interval_ms, now,
            ))
            return cursor.rowcount

    def _claim(self, now: int, limit: int,
               context: RunContext | None = None) -> list[str]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
            db.execute("""
                UPDATE solana_market_refresh_jobs
                SET status = 'retry', lease_until_ms = NULL,
                    claimed_by_owner_id = NULL, claim_fencing_token = NULL,
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
                        last_attempt_at_ms = ?, lease_until_ms = ?, updated_at_ms = ?,
                        claimed_by_owner_id = ?, claim_fencing_token = ?
                    WHERE mint = ? AND status IN ('active', 'retry')
                """, (
                    now, now + self.lease_ms, now,
                    context.owner_id if context else None,
                    context.fencing_token if context else None,
                    mint,
                ))
            return mints

    def _succeed(self, mint: str, now: int, context: RunContext | None = None,
                 inserted: bool | None = None) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
                self._assert_claim(db, mint, now, context)
            cursor = db.execute("""
                UPDATE solana_market_refresh_jobs
                SET status = 'active', consecutive_failures = 0,
                    next_refresh_at_ms = ?, lease_until_ms = NULL,
                    claimed_by_owner_id = NULL, claim_fencing_token = NULL,
                    last_success_at_ms = ?, last_error = NULL, updated_at_ms = ?
                WHERE mint = ? AND status = 'processing'
                  AND (? IS NULL OR (claimed_by_owner_id=? AND claim_fencing_token=?))
            """, (
                now + self.refresh_interval_ms, now, now, mint,
                context.owner_id if context else None,
                context.owner_id if context else None,
                context.fencing_token if context else None,
            ))
            if cursor.rowcount != 1:
                raise LeaseBusy("market refresh completion lost its row claim")
        return bool(inserted)

    def _fail(self, mint: str, error: Exception, now: int,
              context: RunContext | None = None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
                self._assert_claim(db, mint, now, context)
            row = db.execute(
                "SELECT consecutive_failures FROM solana_market_refresh_jobs WHERE mint = ?",
                (mint,),
            ).fetchone()
            failures = (int(row["consecutive_failures"]) if row else 0) + 1
            delay = min(self.retry_base_ms * (2 ** min(failures - 1, 30)), self.retry_max_ms)
            cursor = db.execute("""
                UPDATE solana_market_refresh_jobs
                SET status = 'retry', consecutive_failures = ?,
                    next_refresh_at_ms = ?, lease_until_ms = NULL,
                    claimed_by_owner_id = NULL, claim_fencing_token = NULL,
                    last_error = ?, updated_at_ms = ?
                WHERE mint = ? AND status = 'processing'
                  AND (? IS NULL OR (claimed_by_owner_id=? AND claim_fencing_token=?))
            """, (
                failures, now + delay, f"{type(error).__name__}: {error}"[:500],
                now, mint,
                context.owner_id if context else None,
                context.owner_id if context else None,
                context.fencing_token if context else None,
            ))
            if cursor.rowcount != 1:
                raise LeaseBusy("market refresh failure update lost its row claim")

    def run_once(self, *, limit: int = 25, context: RunContext | None = None) -> MarketRefreshBatchResult:
        """Discover refresh targets and process one bounded batch via batch API."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 300:
            raise ValueError("limit must be between 1 and 300")
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("clock_ms must return a positive integer")
        discovered = self._discover_jobs(now, context)
        claimed = self._claim(now, limit, context)
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
                self._fail(mint, exc, self.clock_ms(), context)
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
                        context,
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
                    if context is not None:
                        context.checkpoint(items=1)
                    with self._connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        if context is not None:
                            context.assert_fence_in_transaction(db)
                            self._assert_claim(db, mint, observed_at, context)
                        inserted = self.market_store.record_in_transaction(snapshot, db)
                        cursor = db.execute("""
                            UPDATE solana_market_refresh_jobs
                            SET status='active', consecutive_failures=0,
                                next_refresh_at_ms=?, lease_until_ms=NULL,
                                claimed_by_owner_id=NULL, claim_fencing_token=NULL,
                                last_success_at_ms=?, last_error=NULL, updated_at_ms=?
                            WHERE mint=? AND status='processing'
                              AND (? IS NULL OR (claimed_by_owner_id=? AND claim_fencing_token=?))
                        """, (
                            observed_at + self.refresh_interval_ms, observed_at, observed_at, mint,
                            context.owner_id if context else None,
                            context.owner_id if context else None,
                            context.fencing_token if context else None,
                        ))
                        if cursor.rowcount != 1:
                            raise LeaseBusy("market refresh completion lost its row claim")
                    if inserted:
                        refreshed += 1
                    else:
                        unchanged += 1
                except Exception as exc:
                    self._fail(mint, exc, self.clock_ms(), context)
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
