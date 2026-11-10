"""Scheduled, retry-safe collection of Solana security evidence."""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ..database import connect_database, row_factory_for
from ..orchestration import LeaseBusy, RunContext

from .launch_stream import LaunchStore
from .market_store import MarketSnapshotStore
from .risk_evidence import SolanaRiskEvidenceCollector
from .risk_store import SolanaRiskEvidenceStore


@dataclass(frozen=True)
class RiskEvidenceBatchResult:
    discovered: int
    processed: int
    recorded: int
    failed: int
    deferred: int


class SolanaRiskEvidenceWorker:
    """Collect risk evidence only for candidates with market-intelligence history."""

    def __init__(
        self,
        launch_store: LaunchStore,
        market_store: MarketSnapshotStore,
        collector: SolanaRiskEvidenceCollector,
        risk_store: SolanaRiskEvidenceStore,
        *,
        refresh_interval_ms: int = 300_000,
        lease_ms: int = 180_000,
        retry_base_ms: int = 10_000,
        retry_max_ms: int = 600_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        paths = {
            launch_store.database_path,
            market_store.database_path,
            risk_store.database_path,
        }
        if len(paths) != 1 or getattr(launch_store, "_uri", False):
            raise ValueError("launch, market and risk stores must share one persistent database")
        for name, value in (
            ("refresh_interval_ms", refresh_interval_ms),
            ("lease_ms", lease_ms),
            ("retry_base_ms", retry_base_ms),
            ("retry_max_ms", retry_max_ms),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if retry_max_ms < retry_base_ms:
            raise ValueError("retry_max_ms must be >= retry_base_ms")
        self.launch_store = launch_store
        self.market_store = market_store
        self.collector = collector
        self.risk_store = risk_store
        self.database_path = launch_store.database_path
        self.refresh_interval_ms = refresh_interval_ms
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
                CREATE TABLE IF NOT EXISTS solana_risk_evidence_jobs (
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
                "CREATE INDEX IF NOT EXISTS idx_solana_risk_evidence_due "
                "ON solana_risk_evidence_jobs(status, next_refresh_at_ms, lease_until_ms)"
            )
            columns = {row["name"] for row in db.execute(
                "PRAGMA table_info(solana_risk_evidence_jobs)"
            ).fetchall()}
            if "claimed_by_owner_id" not in columns:
                db.execute("ALTER TABLE solana_risk_evidence_jobs ADD COLUMN claimed_by_owner_id TEXT")
            if "claim_fencing_token" not in columns:
                db.execute("ALTER TABLE solana_risk_evidence_jobs ADD COLUMN claim_fencing_token INTEGER")

    @staticmethod
    def _assert_claim(db: Any, mint: str, now: int, context: RunContext) -> None:
        if not context.owner_id:
            raise LeaseBusy("run context has no lease owner identity")
        row = db.execute(
            """SELECT 1 FROM solana_risk_evidence_jobs
               WHERE mint=? AND status='processing' AND claimed_by_owner_id=?
                 AND claim_fencing_token=? AND lease_until_ms>?""",
            (mint, context.owner_id, context.fencing_token, now),
        ).fetchone()
        if row is None:
            raise LeaseBusy("risk evidence row claim expired or was replaced")

    def _discover_jobs(self, now: int, context: RunContext | None = None) -> int:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_risk_evidence_jobs
                    (mint, status, attempts, consecutive_failures,
                     next_refresh_at_ms, updated_at_ms)
                SELECT c.mint, 'active', 0, 0, ?, ?
                FROM solana_launch_candidates AS c
                JOIN solana_market_snapshots AS s ON s.mint = c.mint
                GROUP BY c.mint
            """, (now, now))
            return cursor.rowcount

    def _claim(self, now: int, limit: int,
               context: RunContext | None = None) -> list[str]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
            db.execute("""
                UPDATE solana_risk_evidence_jobs
                SET status = 'retry', lease_until_ms = NULL,
                    claimed_by_owner_id = NULL, claim_fencing_token = NULL,
                    next_refresh_at_ms = ?, updated_at_ms = ?
                WHERE status = 'processing' AND lease_until_ms <= ?
            """, (now, now, now))
            rows = db.execute("""
                SELECT mint FROM solana_risk_evidence_jobs
                WHERE status IN ('active', 'retry') AND next_refresh_at_ms <= ?
                ORDER BY next_refresh_at_ms ASC, mint ASC LIMIT ?
            """, (now, limit)).fetchall()
            mints = [row["mint"] for row in rows]
            for mint in mints:
                db.execute("""
                    UPDATE solana_risk_evidence_jobs
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

    def _succeed(self, report: Any, now: int,
                 context: RunContext | None = None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
                self._assert_claim(db, report.mint, now, context)
            self.risk_store.record_in_transaction(report, db)
            degraded = bool(report.errors)
            cursor = db.execute("""
                UPDATE solana_risk_evidence_jobs
                SET status = 'active', consecutive_failures = ?,
                    next_refresh_at_ms = ?, lease_until_ms = NULL,
                    claimed_by_owner_id = NULL, claim_fencing_token = NULL,
                    last_success_at_ms = ?, last_error = ?, updated_at_ms = ?
                WHERE mint = ? AND status = 'processing'
                  AND (? IS NULL OR (claimed_by_owner_id=? AND claim_fencing_token=?))
            """, (
                1 if degraded else 0,
                now + (self.retry_base_ms if degraded else self.refresh_interval_ms),
                now,
                "; ".join(report.errors)[:500] if degraded else None,
                now,
                report.mint,
                context.owner_id if context else None,
                context.owner_id if context else None,
                context.fencing_token if context else None,
            ))
            if cursor.rowcount != 1:
                raise LeaseBusy("risk evidence completion lost its row claim")

    def _fail(self, mint: str, error: Exception, now: int,
              context: RunContext | None = None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
                self._assert_claim(db, mint, now, context)
            row = db.execute(
                "SELECT consecutive_failures FROM solana_risk_evidence_jobs WHERE mint = ?",
                (mint,),
            ).fetchone()
            failures = (int(row["consecutive_failures"]) if row else 0) + 1
            delay = min(
                self.retry_base_ms * (2 ** min(failures - 1, 30)),
                self.retry_max_ms,
            )
            cursor = db.execute("""
                UPDATE solana_risk_evidence_jobs
                SET status = 'retry', consecutive_failures = ?,
                    next_refresh_at_ms = ?, lease_until_ms = NULL,
                    claimed_by_owner_id = NULL, claim_fencing_token = NULL,
                    last_error = ?, updated_at_ms = ?
                WHERE mint = ? AND status = 'processing'
                  AND (? IS NULL OR (claimed_by_owner_id=? AND claim_fencing_token=?))
            """, (
                failures, now + delay,
                f"{type(error).__name__}: {error}"[:500], now, mint,
                context.owner_id if context else None,
                context.owner_id if context else None,
                context.fencing_token if context else None,
            ))
            if cursor.rowcount != 1:
                raise LeaseBusy("risk evidence failure update lost its row claim")

    def run_once(self, *, limit: int = 5, context: RunContext | None = None) -> RiskEvidenceBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25")
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("clock_ms must return a positive integer")
        discovered = self._discover_jobs(now, context)
        claimed = self._claim(now, limit, context)
        recorded = failed = 0
        if context is not None and claimed:
            context.checkpoint(items=len(claimed))
        for mint in claimed:
            try:
                report = self.collector.collect(mint, now_ms=self.clock_ms())
                self._succeed(report, self.clock_ms(), context)
                recorded += 1
            except Exception as exc:
                self._fail(mint, exc, self.clock_ms(), context)
                failed += 1

        with self._connect() as db:
            deferred = int(db.execute("""
                SELECT COUNT(*) FROM solana_risk_evidence_jobs
                WHERE status IN ('active', 'retry', 'processing')
            """).fetchone()[0])
        return RiskEvidenceBatchResult(
            discovered=discovered,
            processed=len(claimed),
            recorded=recorded,
            failed=failed,
            deferred=deferred,
        )

    def status(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute("""
                SELECT status, COUNT(*) AS count
                FROM solana_risk_evidence_jobs GROUP BY status
            """).fetchall()
            totals = db.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN last_success_at_ms IS NOT NULL THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) AS with_provider_errors
                FROM solana_risk_evidence_jobs
            """).fetchone()
        result = {"active": 0, "processing": 0, "retry": 0}
        result.update({row["status"]: int(row["count"]) for row in rows})
        result["total"] = int(totals["total"] or 0)
        result["succeeded"] = int(totals["succeeded"] or 0)
        result["with_provider_errors"] = int(totals["with_provider_errors"] or 0)
        return result
