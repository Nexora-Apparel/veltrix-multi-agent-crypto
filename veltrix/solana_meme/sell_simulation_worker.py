"""Bounded worker for explicitly configured sell-simulation test cases only."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from .candidates import canonical_mint
from .sellability_store import SolanaSellabilityEvidenceStore
from .sell_simulation_store import SolanaSellSimulationStore


@dataclass(frozen=True)
class SellSimulationBatchResult:
    processed: int
    succeeded: int
    simulation_failed: int
    provider_failed: int
    deferred: int


class SolanaSellSimulationWorker:
    """Simulate only mint/amount pairs explicitly supplied by an operator."""

    def __init__(
        self, database_path: str, provider, evidence_store: SolanaSellSimulationStore,
        test_cases: Mapping[str, int], *,
        refresh_interval_ms: int = 300_000,
        retry_base_ms: int = 30_000,
        retry_max_ms: int = 900_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(database_path, str) or not database_path.strip() or database_path == ":memory:":
            raise ValueError("simulation worker requires a persistent SQLite database")
        if evidence_store.database_path != database_path:
            raise ValueError("simulation worker and evidence store must share one database")
        if not isinstance(test_cases, Mapping) or not test_cases:
            raise ValueError("explicit simulation test cases are required")
        normalized = {}
        for mint, amount in test_cases.items():
            canonical = canonical_mint(mint)
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise ValueError("each simulation amount must be a positive raw integer")
            if canonical in normalized:
                raise ValueError("duplicate canonical simulation mint")
            normalized[canonical] = amount
        if refresh_interval_ms < 1 or retry_base_ms < 1 or retry_max_ms < retry_base_ms:
            raise ValueError("invalid simulation worker timing")
        self.database_path = database_path
        self.provider = provider
        self.evidence_store = evidence_store
        self.test_cases = normalized
        self.refresh_interval_ms = refresh_interval_ms
        self.retry_base_ms = retry_base_ms
        self.retry_max_ms = retry_max_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_sell_simulation_jobs (
                    mint TEXT PRIMARY KEY,
                    input_amount_raw INTEGER NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0,
                    next_run_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            db.executemany("""
                INSERT INTO solana_sell_simulation_jobs
                    (mint, input_amount_raw, next_run_at_ms, updated_at_ms)
                VALUES (?, ?, 0, 0)
                ON CONFLICT(mint) DO UPDATE SET input_amount_raw=excluded.input_amount_raw
            """, list(normalized.items()))

    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def run_once(self, *, limit: int = 1) -> SellSimulationBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError("limit must be between 1 and 10")
        now = self.clock_ms()
        with self._connect() as db:
            rows = db.execute("""
                SELECT mint, input_amount_raw FROM solana_sell_simulation_jobs
                WHERE next_run_at_ms <= ? ORDER BY next_run_at_ms, mint LIMIT ?
            """, (now, limit)).fetchall()
        succeeded = simulation_failed = provider_failed = 0
        for row in rows:
            mint, amount = row["mint"], int(row["input_amount_raw"])
            try:
                observation = self.provider.simulate_sell(mint, amount)
                if observation.mint != mint or observation.taker != self.provider.taker:
                    raise ValueError("simulation provider returned mismatched mint or taker")
                if observation.transaction_submitted or observation.signature_verification:
                    raise ValueError("provider violated unsigned simulation boundary")
                self.evidence_store.record(observation)
                if observation.state == "SIMULATION_SUCCEEDED":
                    succeeded += 1
                else:
                    simulation_failed += 1
                failures = 0
                delay = self.refresh_interval_ms
            except Exception:
                provider_failed += 1
                with self._connect() as db:
                    current = db.execute(
                        "SELECT failures FROM solana_sell_simulation_jobs WHERE mint=?", (mint,)
                    ).fetchone()
                failures = (int(current["failures"]) if current else 0) + 1
                delay = min(self.retry_base_ms * (2 ** (failures - 1)), self.retry_max_ms)
            with self._connect() as db:
                db.execute("""
                    UPDATE solana_sell_simulation_jobs
                    SET failures=?, next_run_at_ms=?, updated_at_ms=? WHERE mint=?
                """, (failures, now + delay, now, mint))
        with self._connect() as db:
            deferred = int(db.execute("""
                SELECT COUNT(*) FROM solana_sell_simulation_jobs WHERE next_run_at_ms > ?
            """, (now,)).fetchone()[0])
        return SellSimulationBatchResult(
            len(rows), succeeded, simulation_failed, provider_failed, deferred,
        )
