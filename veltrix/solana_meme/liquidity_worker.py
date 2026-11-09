"""Bounded read-only worker for finalized Solana liquidity inventory."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from ..liquidity_evidence import LiquidityEvidenceAnalyzer
from .launch_stream import LaunchStore
from .liquidity_store import SolanaLiquidityEvidenceStore


@dataclass(frozen=True)
class SolanaLiquidityBatchResult:
    discovered: int
    processed: int
    stored: int
    passed: int
    failed: int
    deferred: int


class SolanaLiquidityEvidenceWorker:
    """Collect and persist liquidity evidence; it never grants trade approval."""

    def __init__(
        self,
        launch_store: LaunchStore,
        provider,
        evidence_store: SolanaLiquidityEvidenceStore,
        *,
        refresh_interval_ms: int = 300_000,
        lease_ms: int = 120_000,
        retry_base_ms: int = 15_000,
        retry_max_ms: int = 900_000,
        max_age_ms: int = 300_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if launch_store.database_path != evidence_store.database_path:
            raise ValueError("launch and liquidity evidence stores must share one SQLite database")
        if launch_store.database_path == ":memory:":
            raise ValueError("liquidity evidence worker requires persistent SQLite storage")
        if not getattr(provider, "provider_id", None) or not callable(
            getattr(provider, "fetch_inventory", None)
        ):
            raise ValueError("a typed liquidity inventory provider is required")
        if min(refresh_interval_ms, lease_ms, retry_base_ms, max_age_ms) < 1:
            raise ValueError("worker timing bounds must be positive")
        if retry_max_ms < retry_base_ms:
            raise ValueError("retry_max_ms must be >= retry_base_ms")
        self.launch_store = launch_store
        self.provider = provider
        self.evidence_store = evidence_store
        self.database_path = launch_store.database_path
        self.refresh_interval_ms = refresh_interval_ms
        self.lease_ms = lease_ms
        self.retry_base_ms = retry_base_ms
        self.retry_max_ms = retry_max_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.analyzer = LiquidityEvidenceAnalyzer(max_age_ms=max_age_ms)
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self):
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_liquidity_jobs (
                    mint TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('active', 'processing', 'retry')),
                    failures INTEGER NOT NULL DEFAULT 0,
                    next_collect_at_ms INTEGER NOT NULL,
                    lease_until_ms INTEGER,
                    last_success_at_ms INTEGER,
                    last_error TEXT,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_liquidity_jobs_due "
                "ON solana_liquidity_jobs(status, next_collect_at_ms, lease_until_ms)"
            )

    def _discover(self, now):
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_liquidity_jobs
                    (mint, status, failures, next_collect_at_ms, updated_at_ms)
                SELECT c.mint, 'active', 0, ?, ?
                FROM solana_launch_candidates AS c
                WHERE EXISTS (
                    SELECT 1 FROM solana_market_snapshots AS s WHERE s.mint = c.mint
                )
            """, (now, now))
            return cursor.rowcount

    def _claim(self, now, limit):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE solana_liquidity_jobs
                SET status = 'retry', lease_until_ms = NULL,
                    next_collect_at_ms = ?, updated_at_ms = ?
                WHERE status = 'processing' AND lease_until_ms <= ?
            """, (now, now, now))
            rows = db.execute("""
                SELECT mint FROM solana_liquidity_jobs
                WHERE status IN ('active', 'retry') AND next_collect_at_ms <= ?
                ORDER BY next_collect_at_ms, mint LIMIT ?
            """, (now, limit)).fetchall()
            mints = [row["mint"] for row in rows]
            for mint in mints:
                db.execute("""
                    UPDATE solana_liquidity_jobs
                    SET status = 'processing', lease_until_ms = ?, updated_at_ms = ?
                    WHERE mint = ?
                """, (now + self.lease_ms, now, mint))
            return mints

    def _succeed(self, mint, now):
        with self._connect() as db:
            db.execute("""
                UPDATE solana_liquidity_jobs
                SET status = 'active', failures = 0, next_collect_at_ms = ?,
                    lease_until_ms = NULL, last_success_at_ms = ?,
                    last_error = NULL, updated_at_ms = ?
                WHERE mint = ?
            """, (now + self.refresh_interval_ms, now, now, mint))

    def _fail(self, mint, error, now):
        with self._connect() as db:
            row = db.execute(
                "SELECT failures FROM solana_liquidity_jobs WHERE mint = ?", (mint,)
            ).fetchone()
            failures = (int(row["failures"]) if row else 0) + 1
            delay = min(self.retry_base_ms * (2 ** (failures - 1)), self.retry_max_ms)
            db.execute("""
                UPDATE solana_liquidity_jobs
                SET status = 'retry', failures = ?, next_collect_at_ms = ?,
                    lease_until_ms = NULL, last_error = ?, updated_at_ms = ?
                WHERE mint = ?
            """, (failures, now + delay, f"{type(error).__name__}: {error}"[:300], now, mint))

    def run_once(self, *, limit: int = 2) -> SolanaLiquidityBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        now = self.clock_ms()
        discovered = self._discover(now)
        claimed = self._claim(now, limit)
        stored = passed = failed = 0
        for mint in claimed:
            try:
                batch = self.provider.fetch_inventory(mint)
                report = self.analyzer.evaluate(
                    batch, chain="solana", token_address=mint, now_ms=self.clock_ms(),
                )
                self.evidence_store.record(report)
                stored += 1
                passed += int(report.passed)
                self._succeed(mint, self.clock_ms())
            except Exception as exc:
                self._fail(mint, exc, self.clock_ms())
                failed += 1
        with self._connect() as db:
            deferred = int(db.execute("""
                SELECT COUNT(*) FROM solana_liquidity_jobs
                WHERE status IN ('active', 'processing', 'retry')
            """).fetchone()[0])
        return SolanaLiquidityBatchResult(
            discovered=discovered, processed=len(claimed), stored=stored,
            passed=passed, failed=failed, deferred=deferred,
        )

    def status(self):
        with self._connect() as db:
            rows = db.execute("""
                SELECT status, COUNT(*) AS count
                FROM solana_liquidity_jobs GROUP BY status
            """).fetchall()
        result = {"active": 0, "processing": 0, "retry": 0}
        result.update({row["status"]: int(row["count"]) for row in rows})
        return result
