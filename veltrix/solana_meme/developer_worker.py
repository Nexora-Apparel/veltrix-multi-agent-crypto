"""Read-only worker for signed-root developer funding ancestry evidence."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from ..developer_concentration import DeveloperConcentrationAnalyzer, DeveloperConcentrationReport
from ..developer_evidence import DeveloperRootAttestation
from .launch_stream import LaunchStore
from .developer_store import DeveloperConcentrationStore


@dataclass(frozen=True)
class DeveloperEvidenceBatchResult:
    discovered: int
    processed: int
    stored: int
    incomplete: int
    failed: int
    deferred: int


class SolanaDeveloperEvidenceWorker:
    """Trace holder funding to independently attested roots; never certifies safety."""

    def __init__(
        self,
        launch_store: LaunchStore,
        holder_provider,
        funding_provider,
        root_provider,
        evidence_store: DeveloperConcentrationStore,
        *,
        trusted_holder_providers,
        trusted_funding_providers,
        trusted_root_providers,
        refresh_interval_ms: int = 900_000,
        lease_ms: int = 180_000,
        retry_base_ms: int = 15_000,
        retry_max_ms: int = 900_000,
        max_age_ms: int = 300_000,
        max_ancestry_depth: int = 8,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if launch_store.database_path != evidence_store.database_path:
            raise ValueError("launch and developer evidence stores must share one SQLite database")
        if launch_store.database_path == ":memory:":
            raise ValueError("developer evidence worker requires a persistent SQLite database")
        if min(refresh_interval_ms, lease_ms, retry_base_ms, max_age_ms) < 1:
            raise ValueError("refresh, lease, retry, and evidence-age settings must be positive")
        if retry_max_ms < retry_base_ms:
            raise ValueError("retry_max_ms must be >= retry_base_ms")
        if not trusted_holder_providers or holder_provider.provider_id not in trusted_holder_providers:
            raise ValueError("holder provider must be explicitly trusted")
        if not trusted_funding_providers or funding_provider.provider_id not in trusted_funding_providers:
            raise ValueError("funding provider must be explicitly trusted")
        if not trusted_root_providers or root_provider.provider_id not in trusted_root_providers:
            raise ValueError("signed root provider must be explicitly trusted")
        self.launch_store = launch_store
        self.holder_provider = holder_provider
        self.funding_provider = funding_provider
        self.root_provider = root_provider
        self.evidence_store = evidence_store
        self.database_path = launch_store.database_path
        self.analyzer = DeveloperConcentrationAnalyzer(
            max_age_ms=max_age_ms, max_ancestry_depth=max_ancestry_depth,
        )
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
                CREATE TABLE IF NOT EXISTS solana_developer_evidence_jobs (
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
                "CREATE INDEX IF NOT EXISTS idx_solana_developer_jobs_due "
                "ON solana_developer_evidence_jobs(status, next_collect_at_ms, lease_until_ms)"
            )

    def _discover(self, now):
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_developer_evidence_jobs
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
                UPDATE solana_developer_evidence_jobs
                SET status = 'retry', lease_until_ms = NULL,
                    next_collect_at_ms = ?, updated_at_ms = ?
                WHERE status = 'processing' AND lease_until_ms <= ?
            """, (now, now, now))
            rows = db.execute("""
                SELECT mint FROM solana_developer_evidence_jobs
                WHERE status IN ('active', 'retry') AND next_collect_at_ms <= ?
                ORDER BY next_collect_at_ms, mint LIMIT ?
            """, (now, limit)).fetchall()
            mints = [row["mint"] for row in rows]
            for mint in mints:
                db.execute("""
                    UPDATE solana_developer_evidence_jobs
                    SET status = 'processing', lease_until_ms = ?, updated_at_ms = ?
                    WHERE mint = ?
                """, (now + self.lease_ms, now, mint))
            return mints

    @staticmethod
    def _blocked(mint, now, reason):
        return DeveloperConcentrationReport(
            chain="solana", token_address=mint, provider=None,
            linked_wallets=(), linked_supply_raw=None, total_supply_raw=None,
            dev_hold_pct=None, coverage_complete=False, observed_at_ms=now,
            evidence_id=None, source_ref=None,
            method="blocked_incomplete_signed_root_or_funding_evidence",
            independence_group=None, holder_provider=None,
            funding_provider=None, root_provider=None, reasons=(reason,),
        )

    def _succeed(self, mint, now):
        with self._connect() as db:
            db.execute("""
                UPDATE solana_developer_evidence_jobs
                SET status = 'active', failures = 0, next_collect_at_ms = ?,
                    lease_until_ms = NULL, last_success_at_ms = ?,
                    last_error = NULL, updated_at_ms = ?
                WHERE mint = ?
            """, (now + self.refresh_interval_ms, now, now, mint))

    def _fail(self, mint, error, now):
        with self._connect() as db:
            row = db.execute(
                "SELECT failures FROM solana_developer_evidence_jobs WHERE mint = ?",
                (mint,),
            ).fetchone()
            failures = (int(row["failures"]) if row else 0) + 1
            delay = min(self.retry_base_ms * (2 ** (failures - 1)), self.retry_max_ms)
            db.execute("""
                UPDATE solana_developer_evidence_jobs
                SET status = 'retry', failures = ?, next_collect_at_ms = ?,
                    lease_until_ms = NULL, last_error = ?, updated_at_ms = ?
                WHERE mint = ?
            """, (failures, now + delay, f"{type(error).__name__}: {error}"[:300], now, mint))

    def _collect(self, mint, now):
        roots = self.root_provider.fetch_attestation("solana", mint)
        if not isinstance(roots, DeveloperRootAttestation):
            raise TypeError("root provider returned an untyped attestation")
        if roots.provider != self.root_provider.provider_id or roots.signature_verified is not True:
            raise ValueError("developer-root signature or provider identity was not verified")
        if roots.coverage_complete is not True or not roots.roots:
            raise ValueError("developer-root coverage is incomplete")
        holder = self.holder_provider.fetch_snapshot(mint)
        if holder.provider != self.holder_provider.provider_id:
            raise ValueError("holder provider identity mismatch")
        funding = self.funding_provider.fetch_snapshot(
            mint, [row.wallet_address for row in holder.holders],
        )
        if funding.provider != self.funding_provider.provider_id:
            raise ValueError("funding provider identity mismatch")
        return self.analyzer.analyze(
            "solana", mint, holder, funding, roots.roots,
            root_provider=roots.provider, root_evidence_id=roots.evidence_id,
            now_ms=now, certified_provider_id=None,
        )

    def run_once(self, *, limit: int = 3) -> DeveloperEvidenceBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25")
        now = self.clock_ms()
        discovered = self._discover(now)
        claimed = self._claim(now, limit)
        stored = incomplete = failed = 0
        for mint in claimed:
            try:
                report = self._collect(mint, self.clock_ms())
                self.evidence_store.record(report)
                self._succeed(mint, self.clock_ms())
                stored += 1
                if not report.coverage_complete:
                    incomplete += 1
            except Exception as exc:
                self.evidence_store.record(self._blocked(
                    mint, self.clock_ms(), f"evidence collection rejected: {type(exc).__name__}",
                ))
                self._fail(mint, exc, self.clock_ms())
                failed += 1
        with self._connect() as db:
            deferred = int(db.execute("""
                SELECT COUNT(*) FROM solana_developer_evidence_jobs
                WHERE status IN ('active', 'processing', 'retry')
            """).fetchone()[0])
        return DeveloperEvidenceBatchResult(
            discovered=discovered, processed=len(claimed), stored=stored,
            incomplete=incomplete, failed=failed, deferred=deferred,
        )

    def status(self):
        with self._connect() as db:
            rows = db.execute("""
                SELECT status, COUNT(*) AS count
                FROM solana_developer_evidence_jobs GROUP BY status
            """).fetchall()
        result = {"active": 0, "processing": 0, "retry": 0}
        result.update({row["status"]: int(row["count"]) for row in rows})
        return result
