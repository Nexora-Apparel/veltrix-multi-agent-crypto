"""Bounded worker for launch-window shared-funder evidence."""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Callable

from ..launch_bundles import LaunchBundleAnalyzer, LaunchBundleReport
from ..launch_intelligence import LaunchIndexSnapshot
from .launch_stream import LaunchStore
from .bundle_store import LaunchBundleEvidenceStore


@dataclass(frozen=True)
class LaunchBundleBatchResult:
    discovered: int
    processed: int
    stored: int
    complete: int
    incomplete: int
    failed: int
    deferred: int


class SolanaLaunchBundleWorker:
    """Collect launch-window buys and funding cohorts; never certifies ownership."""

    def __init__(
        self,
        launch_store: LaunchStore,
        provider,
        analyzer: LaunchBundleAnalyzer,
        evidence_store: LaunchBundleEvidenceStore,
        *,
        refresh_interval_ms: int = 3_600_000,
        lease_ms: int = 180_000,
        retry_base_ms: int = 30_000,
        retry_max_ms: int = 1_800_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if launch_store.database_path != evidence_store.database_path:
            raise ValueError("launch and bundle evidence stores must share one SQLite database")
        if launch_store.database_path == ":memory:":
            raise ValueError("bundle evidence worker requires a persistent SQLite database")
        if min(refresh_interval_ms, lease_ms, retry_base_ms) < 1 or retry_max_ms < retry_base_ms:
            raise ValueError("invalid bundle worker timing configuration")
        if getattr(provider, "provider_id", None) not in analyzer.trusted_providers:
            raise ValueError("launch index provider must be explicitly trusted")
        self.launch_store = launch_store
        self.provider = provider
        self.analyzer = analyzer
        self.evidence_store = evidence_store
        self.database_path = launch_store.database_path
        self.refresh_interval_ms = refresh_interval_ms
        self.lease_ms = lease_ms
        self.retry_base_ms = retry_base_ms
        self.retry_max_ms = retry_max_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._initialize()

    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self):
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_launch_bundle_jobs (
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
                "CREATE INDEX IF NOT EXISTS idx_solana_bundle_jobs_due "
                "ON solana_launch_bundle_jobs(status, next_collect_at_ms, lease_until_ms)"
            )

    def _discover(self, now):
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_launch_bundle_jobs
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
                UPDATE solana_launch_bundle_jobs
                SET status = 'retry', lease_until_ms = NULL,
                    next_collect_at_ms = ?, updated_at_ms = ?
                WHERE status = 'processing' AND lease_until_ms <= ?
            """, (now, now, now))
            rows = db.execute("""
                SELECT mint FROM solana_launch_bundle_jobs
                WHERE status IN ('active', 'retry') AND next_collect_at_ms <= ?
                ORDER BY next_collect_at_ms, mint LIMIT ?
            """, (now, limit)).fetchall()
            mints = [row["mint"] for row in rows]
            for mint in mints:
                db.execute("""
                    UPDATE solana_launch_bundle_jobs
                    SET status = 'processing', lease_until_ms = ?, updated_at_ms = ?
                    WHERE mint = ?
                """, (now + self.lease_ms, now, mint))
            return mints

    @staticmethod
    def _withheld(mint, provider, now, reason):
        return LaunchBundleReport(
            chain="solana", token_address=mint, provider=provider,
            launch_at_ms=0, window_end_ms=0, coverage_complete=False,
            bundled_supply_pct=None, shared_funder_clusters=0,
            participating_wallets=0, accepted_transactions=0,
            rejected=(reason,),
            notes=("Shared funding is a heuristic, not proof of common ownership or insider control.",),
            observed_at_ms=now,
        )

    def _collect(self, mint, now):
        snapshot = self.provider.fetch_launch_snapshot(
            "solana", mint, self.analyzer.launch_window_ms,
        )
        if not isinstance(snapshot, LaunchIndexSnapshot):
            raise TypeError("launch provider returned an untyped snapshot")
        coverage = snapshot.coverage
        coverage_ok = bool(
            snapshot.provider == self.provider.provider_id
            and snapshot.chain == "solana" and snapshot.token_address == mint
            and isinstance(snapshot.launch_at_ms, int) and snapshot.launch_at_ms <= now
            and isinstance(snapshot.observed_at_ms, int) and snapshot.observed_at_ms <= now
            and coverage.provider == self.provider.provider_id
            and coverage.chain == "solana" and coverage.token_address == mint
            and coverage.evidence_id
            and coverage.requested_start_ms == snapshot.launch_at_ms
            and coverage.requested_end_ms == snapshot.launch_at_ms + self.analyzer.launch_window_ms
            and coverage.indexed_through_ms >= coverage.requested_end_ms
            and coverage.pages_expected >= 1
            and coverage.pages_completed == coverage.pages_expected
            and coverage.source_complete is True
            and coverage.requested_end_ms <= now
            and isinstance(snapshot.total_supply, (int, float))
            and not isinstance(snapshot.total_supply, bool)
            and math.isfinite(snapshot.total_supply) and snapshot.total_supply > 0
        )
        report = self.analyzer.analyze(
            provider=snapshot.provider, chain=snapshot.chain,
            token_address=snapshot.token_address, launch_at_ms=snapshot.launch_at_ms,
            total_supply=snapshot.total_supply, observations=snapshot.observations,
            coverage_complete=coverage_ok, now_ms=now,
        )
        return replace(
            report,
            evidence_id=coverage.evidence_id,
            observed_at_ms=snapshot.observed_at_ms,
            scanned_through_ms=coverage.indexed_through_ms,
            supply_source="solana-finalized-mint-account",
            supply_block_ref=snapshot.supply_block_ref,
            supply_observed_at_ms=snapshot.observed_at_ms,
            pages_scanned=coverage.pages_completed,
            funder_providers=tuple(sorted({
                item.funder_provider for item in snapshot.observations
                if item.funder_provider
            })),
            provenance=(coverage.evidence_id, snapshot.supply_block_ref),
        )

    def _succeed(self, mint, now):
        with self._connect() as db:
            db.execute("""
                UPDATE solana_launch_bundle_jobs
                SET status = 'active', failures = 0, next_collect_at_ms = ?,
                    lease_until_ms = NULL, last_success_at_ms = ?,
                    last_error = NULL, updated_at_ms = ?
                WHERE mint = ?
            """, (now + self.refresh_interval_ms, now, now, mint))

    def _fail(self, mint, error, now):
        with self._connect() as db:
            row = db.execute(
                "SELECT failures FROM solana_launch_bundle_jobs WHERE mint = ?",
                (mint,),
            ).fetchone()
            failures = (int(row["failures"]) if row else 0) + 1
            delay = min(self.retry_base_ms * (2 ** (failures - 1)), self.retry_max_ms)
            db.execute("""
                UPDATE solana_launch_bundle_jobs
                SET status = 'retry', failures = ?, next_collect_at_ms = ?,
                    lease_until_ms = NULL, last_error = ?, updated_at_ms = ?
                WHERE mint = ?
            """, (failures, now + delay, f"{type(error).__name__}: {error}"[:300], now, mint))

    def run_once(self, *, limit: int = 1) -> LaunchBundleBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        now = self.clock_ms()
        discovered = self._discover(now)
        claimed = self._claim(now, limit)
        stored = complete = incomplete = failed = 0
        for mint in claimed:
            try:
                report = self._collect(mint, self.clock_ms())
                self.evidence_store.record(report)
                self._succeed(mint, self.clock_ms())
                stored += 1
                if report.coverage_complete and report.bundled_supply_pct is not None:
                    complete += 1
                else:
                    incomplete += 1
            except Exception as exc:
                self.evidence_store.record(self._withheld(
                    mint, self.provider.provider_id, self.clock_ms(),
                    f"launch evidence collection rejected: {type(exc).__name__}",
                ))
                self._fail(mint, exc, self.clock_ms())
                failed += 1
        with self._connect() as db:
            deferred = int(db.execute("""
                SELECT COUNT(*) FROM solana_launch_bundle_jobs
                WHERE status IN ('active', 'processing', 'retry')
            """).fetchone()[0])
        return LaunchBundleBatchResult(
            discovered=discovered, processed=len(claimed), stored=stored,
            complete=complete, incomplete=incomplete, failed=failed, deferred=deferred,
        )

    def status(self):
        with self._connect() as db:
            rows = db.execute("""
                SELECT status, COUNT(*) AS count
                FROM solana_launch_bundle_jobs GROUP BY status
            """).fetchall()
        result = {"active": 0, "processing": 0, "retry": 0}
        result.update({row["status"]: int(row["count"]) for row in rows})
        return result
