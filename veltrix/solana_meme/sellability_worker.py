"""Bounded, retry-safe persistence worker for read-only sell-route observations."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable

from .launch_stream import LaunchStore
from .risk_store import SolanaRiskEvidenceStore
from .sellability import SolanaSellabilityEvaluator
from .paper_sizing import calculate_paper_exit_size
from .sellability_store import SolanaSellabilityEvidenceStore


@dataclass(frozen=True)
class SellabilityBatchResult:
    discovered: int
    processed: int
    stored: int
    route_observed: int
    unverified: int
    no_route: int
    failed: int
    deferred: int


class SolanaSellabilityWorker:
    """Collect bounded sell-route evidence only when finalized mint supply exists."""

    def __init__(
        self,
        launch_store: LaunchStore,
        risk_store: SolanaRiskEvidenceStore,
        evaluator: SolanaSellabilityEvaluator,
        evidence_store: SolanaSellabilityEvidenceStore,
        *,
        refresh_interval_ms: int = 30_000,
        lease_ms: int = 20_000,
        retry_base_ms: int = 5_000,
        retry_max_ms: int = 300_000,
        quote_supply_fraction: float = 0.01,
        market_store=None,
        paper_notional_usd: float | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        paths = {
            launch_store.database_path, risk_store.database_path,
            evidence_store.database_path,
        }
        if len(paths) != 1 or ":memory:" in paths:
            raise ValueError("sellability worker stores must share one persistent SQLite database")
        if min(refresh_interval_ms, lease_ms, retry_base_ms) < 1 or retry_max_ms < retry_base_ms:
            raise ValueError("invalid sellability worker timing")
        if not 0.000001 <= quote_supply_fraction <= 0.05:
            raise ValueError("quote_supply_fraction must be between 0.000001 and 0.05")
        if market_store is not None and market_store.database_path != launch_store.database_path:
            raise ValueError("market store must share the sellability worker database")
        if paper_notional_usd is not None:
            if isinstance(paper_notional_usd, bool) or not isinstance(paper_notional_usd, (int, float)) or not 0 < paper_notional_usd <= 50:
                raise ValueError("paper_notional_usd must be in (0, 50]")
            if market_store is None:
                raise ValueError("market store is required for exact paper-notional sell quotes")
        self.launch_store = launch_store
        self.risk_store = risk_store
        self.evaluator = evaluator
        self.evidence_store = evidence_store
        self.database_path = launch_store.database_path
        self.refresh_interval_ms = refresh_interval_ms
        self.lease_ms = lease_ms
        self.retry_base_ms = retry_base_ms
        self.retry_max_ms = retry_max_ms
        self.quote_supply_fraction = quote_supply_fraction
        self.market_store = market_store
        self.paper_notional_usd = paper_notional_usd
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_sellability_jobs (
                    mint TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('active', 'processing', 'retry')),
                    failures INTEGER NOT NULL DEFAULT 0,
                    next_run_at_ms INTEGER NOT NULL,
                    lease_until_ms INTEGER,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_sellability_due "
                "ON solana_sellability_jobs(status, next_run_at_ms, lease_until_ms)"
            )

    def _eligible_mints(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT mint FROM solana_launch_candidates ORDER BY first_seen_at_ms, mint"
            ).fetchall()
        eligible = []
        for row in rows:
            latest = self.risk_store.latest(row["mint"])
            report = latest.get("report") if latest else None
            if (isinstance(report, dict) and report.get("mint_evidence_id")
                    and report.get("mint_supply_reconciled") is True
                    and isinstance(report.get("mint_supply_raw"), int)
                    and report["mint_supply_raw"] > 0
                    and isinstance(report.get("mint_decimals"), int)
                    and not isinstance(report.get("mint_decimals"), bool)):
                eligible.append(row["mint"])
        return eligible

    def _discover(self, now: int) -> int:
        mints = self._eligible_mints()
        with self._connect() as db:
            before = db.total_changes
            db.executemany("""
                INSERT OR IGNORE INTO solana_sellability_jobs
                    (mint, status, failures, next_run_at_ms, updated_at_ms)
                VALUES (?, 'active', 0, ?, ?)
            """, [(mint, now, now) for mint in mints])
            return db.total_changes - before

    def _claim(self, now: int, limit: int) -> list[str]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE solana_sellability_jobs
                SET status='retry', lease_until_ms=NULL,
                    next_run_at_ms=?, updated_at_ms=?
                WHERE status='processing' AND lease_until_ms <= ?
            """, (now, now, now))
            rows = db.execute("""
                SELECT mint FROM solana_sellability_jobs
                WHERE status IN ('active', 'retry') AND next_run_at_ms <= ?
                ORDER BY next_run_at_ms, mint LIMIT ?
            """, (now, limit)).fetchall()
            mints = [row["mint"] for row in rows]
            for mint in mints:
                db.execute("""
                    UPDATE solana_sellability_jobs
                    SET status='processing', lease_until_ms=?, updated_at_ms=?
                    WHERE mint=?
                """, (now + self.lease_ms, now, mint))
            return mints

    def _finish(self, mint: str, now: int, *, error: Exception | None = None) -> None:
        with self._connect() as db:
            if error is None:
                db.execute("""
                    UPDATE solana_sellability_jobs
                    SET status='active', failures=0, next_run_at_ms=?,
                        lease_until_ms=NULL, updated_at_ms=?
                    WHERE mint=?
                """, (now + self.refresh_interval_ms, now, mint))
            else:
                row = db.execute(
                    "SELECT failures FROM solana_sellability_jobs WHERE mint=?", (mint,)
                ).fetchone()
                failures = (int(row["failures"]) if row else 0) + 1
                delay = min(self.retry_base_ms * (2 ** (failures - 1)), self.retry_max_ms)
                db.execute("""
                    UPDATE solana_sellability_jobs
                    SET status='retry', failures=?, next_run_at_ms=?,
                        lease_until_ms=NULL, updated_at_ms=?
                    WHERE mint=?
                """, (failures, now + delay, now, mint))

    def run_once(self, *, limit: int = 2) -> SellabilityBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        now = self.clock_ms()
        discovered = self._discover(now)
        claimed = self._claim(now, limit)
        stored = route_observed = unverified = no_route = failed = 0
        for mint in claimed:
            try:
                latest = self.risk_store.latest(mint)
                report = latest.get("report") if latest else None
                if (not isinstance(report, dict) or not report.get("mint_evidence_id")
                        or not isinstance(report.get("total_supply_raw"), int)):
                    raise RuntimeError("fresh finalized mint supply evidence is unavailable")
                supply = report["mint_supply_raw"]
                if self.market_store is not None and self.paper_notional_usd is not None:
                    market = self.market_store.latest(mint)
                    if not isinstance(market, dict) or not market.get("price_usd"):
                        raise RuntimeError("fresh market USD price is unavailable for exact paper sizing")
                    size = calculate_paper_exit_size(
                        mint,
                        notional_usd=self.paper_notional_usd,
                        token_price_usd=market["price_usd"],
                        decimals=report["mint_decimals"],
                        total_supply_raw=supply,
                    )
                    amount = size.input_amount_raw
                else:
                    amount = max(1, int(supply * self.quote_supply_fraction))
                evidence = self.evaluator.evaluate(
                    mint, total_supply_raw=supply, input_amount_raw=amount,
                    now_ms=self.clock_ms(),
                )
                self.evidence_store.record(evidence)
                self._finish(mint, self.clock_ms())
                stored += 1
                route_observed += int(evidence.route_observed)
                unverified += int(evidence.state == "UNVERIFIED")
                no_route += int(evidence.state == "NO_SELL_ROUTE_OBSERVED")
            except Exception as exc:
                self._finish(mint, self.clock_ms(), error=exc)
                failed += 1
        with self._connect() as db:
            deferred = int(db.execute("""
                SELECT COUNT(*) FROM solana_sellability_jobs
                WHERE status IN ('active','processing','retry')
            """).fetchone()[0])
        return SellabilityBatchResult(
            discovered, len(claimed), stored, route_observed, unverified,
            no_route, failed, deferred,
        )
