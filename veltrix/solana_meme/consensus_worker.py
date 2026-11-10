"""Scheduled consensus analysis over Phase 04 market and Phase 05 risk evidence."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from ..database import connect_database, row_factory_for

from ..whales import WalletBuyEvent
from .consensus import ConsensusEngine, MarketEvidence, RiskEvidence
from .consensus_store import ConsensusStore
from .launch_stream import LaunchStore
from .market_store import MarketSnapshotStore
from .risk_store import SolanaRiskEvidenceStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConsensusBatchResult:
    discovered: int
    processed: int
    recorded: int
    unchanged: int
    failed: int
    deferred: int


class ConsensusWorker:
    """Generate analytical decisions; never creates paper or live positions."""

    def __init__(
        self,
        launch_store: LaunchStore,
        market_store: MarketSnapshotStore,
        risk_store: SolanaRiskEvidenceStore,
        engine: ConsensusEngine,
        consensus_store: ConsensusStore,
        *,
        refresh_interval_ms: int = 60_000,
        lease_ms: int = 90_000,
        retry_base_ms: int = 5_000,
        retry_max_ms: int = 300_000,
        clock_ms: Callable[[], int] | None = None,
        wallet_event_provider: Callable[[str], Iterable[WalletBuyEvent]] | None = None,
        wallet_event_batch_provider: Callable[[Iterable[str], int], Iterable[WalletBuyEvent]] | None = None,
    ) -> None:
        paths = {
            launch_store.database_path,
            market_store.database_path,
            risk_store.database_path,
            consensus_store.database_path,
        }
        if len(paths) != 1 or getattr(launch_store, "_uri", False):
            raise ValueError("launch, market, risk and consensus stores must share one persistent database")
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
        self.risk_store = risk_store
        self.engine = engine
        self.consensus_store = consensus_store
        self.database_path = launch_store.database_path
        self.refresh_interval_ms = refresh_interval_ms
        self.lease_ms = lease_ms
        self.retry_base_ms = retry_base_ms
        self.retry_max_ms = retry_max_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.wallet_event_provider = wallet_event_provider
        self.wallet_event_batch_provider = wallet_event_batch_provider
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
                CREATE TABLE IF NOT EXISTS solana_consensus_jobs (
                    mint TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('active', 'processing', 'retry')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    next_run_at_ms INTEGER NOT NULL,
                    lease_until_ms INTEGER,
                    last_attempt_at_ms INTEGER,
                    last_success_at_ms INTEGER,
                    last_error TEXT,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_consensus_due "
                "ON solana_consensus_jobs(status, next_run_at_ms, lease_until_ms)"
            )

    def _discover_jobs(self, now: int) -> int:
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_consensus_jobs
                    (mint, status, attempts, consecutive_failures, next_run_at_ms, updated_at_ms)
                SELECT c.mint, 'active', 0, 0, ?, ?
                FROM solana_launch_candidates AS c
                JOIN solana_market_snapshots AS m ON m.mint = c.mint
                JOIN solana_risk_evidence_reports AS r ON r.mint = c.mint
                GROUP BY c.mint
            """, (now, now))
            return cursor.rowcount

    def _claim(self, now: int, limit: int) -> list[str]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                UPDATE solana_consensus_jobs
                SET status = 'retry', lease_until_ms = NULL,
                    next_run_at_ms = ?, updated_at_ms = ?
                WHERE status = 'processing' AND lease_until_ms <= ?
            """, (now, now, now))
            rows = db.execute("""
                SELECT mint FROM solana_consensus_jobs
                WHERE status IN ('active', 'retry') AND next_run_at_ms <= ?
                ORDER BY next_run_at_ms ASC, mint ASC LIMIT ?
            """, (now, limit)).fetchall()
            mints = [row["mint"] for row in rows]
            for mint in mints:
                db.execute("""
                    UPDATE solana_consensus_jobs
                    SET status = 'processing', attempts = attempts + 1,
                        last_attempt_at_ms = ?, lease_until_ms = ?, updated_at_ms = ?
                    WHERE mint = ?
                """, (now, now + self.lease_ms, now, mint))
            return mints

    def _succeed(self, mint: str, now: int) -> None:
        with self._connect() as db:
            db.execute("""
                UPDATE solana_consensus_jobs
                SET status = 'active', consecutive_failures = 0,
                    next_run_at_ms = ?, lease_until_ms = NULL,
                    last_success_at_ms = ?, last_error = NULL, updated_at_ms = ?
                WHERE mint = ?
            """, (now + self.refresh_interval_ms, now, now, mint))

    def _fail(self, mint: str, error: Exception, now: int) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT consecutive_failures FROM solana_consensus_jobs WHERE mint = ?",
                (mint,),
            ).fetchone()
            failures = (int(row["consecutive_failures"]) if row else 0) + 1
            delay = min(self.retry_base_ms * (2 ** min(failures - 1, 30)), self.retry_max_ms)
            db.execute("""
                UPDATE solana_consensus_jobs
                SET status = 'retry', consecutive_failures = ?,
                    next_run_at_ms = ?, lease_until_ms = NULL,
                    last_error = ?, updated_at_ms = ?
                WHERE mint = ?
            """, (failures, now + delay, f"{type(error).__name__}: {error}"[:500], now, mint))

    def _wallet_events(self, mint: str) -> tuple[WalletBuyEvent, ...]:
        if self.wallet_event_provider is None:
            return ()
        events = self.wallet_event_provider(mint)
        if events is None:
            return ()
        return tuple(event for event in events if isinstance(event, WalletBuyEvent))

    def _wallet_events_by_mint(self, mints: Iterable[str], now_ms: int) -> dict[str, tuple[WalletBuyEvent, ...]]:
        if self.wallet_event_batch_provider is None:
            return {}
        try:
            events = tuple(self.wallet_event_batch_provider(tuple(mints), now_ms))
        except Exception as exc:
            LOGGER.warning("Solana wallet evidence batch unavailable; Whale will abstain: %s", exc)
            return {}
        grouped: dict[str, list[WalletBuyEvent]] = {}
        for event in events:
            if not isinstance(event, WalletBuyEvent) or event.chain != "solana":
                continue
            grouped.setdefault(event.token_address, []).append(event)
        return {mint: tuple(rows) for mint, rows in grouped.items()}

    def run_once(self, *, limit: int = 10) -> ConsensusBatchResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25")
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("clock_ms must return a positive integer")
        discovered = self._discover_jobs(now)
        claimed = self._claim(now, limit)
        recorded = unchanged = failed = 0
        wallet_events_by_mint = self._wallet_events_by_mint(claimed, now)

        for mint in claimed:
            try:
                market_row = self.market_store.latest(mint)
                risk_payload = self.risk_store.latest(mint)
                if market_row is None or risk_payload is None:
                    raise ValueError("market or risk evidence disappeared before consensus")
                market = MarketEvidence.from_store_row(market_row)
                risk = RiskEvidence.from_store_payload(risk_payload)
                history_rows = self.market_store.history(mint, limit=20)
                prior_history = [
                    row for row in history_rows
                    if row.get("id") != market.snapshot_id
                    and isinstance(row.get("retrieved_at_ms"), int)
                    and row["retrieved_at_ms"] < market.retrieved_at_ms
                ]
                decision = self.engine.evaluate(
                    market,
                    risk,
                    history=prior_history,
                    wallet_events=(wallet_events_by_mint.get(mint, ()) if self.wallet_event_batch_provider is not None else self._wallet_events(mint)),
                    now_ms=self.clock_ms(),
                )
                if self.consensus_store.record(decision):
                    recorded += 1
                else:
                    unchanged += 1
                self._succeed(mint, self.clock_ms())
            except Exception as exc:
                self._fail(mint, exc, self.clock_ms())
                failed += 1

        with self._connect() as db:
            deferred = int(db.execute("""
                SELECT COUNT(*) FROM solana_consensus_jobs
                WHERE status IN ('active', 'retry', 'processing')
            """).fetchone()[0])
        return ConsensusBatchResult(
            discovered=discovered,
            processed=len(claimed),
            recorded=recorded,
            unchanged=unchanged,
            failed=failed,
            deferred=deferred,
        )

    def status(self) -> dict[str, int]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS count FROM solana_consensus_jobs GROUP BY status"
            ).fetchall()
            totals = db.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN last_success_at_ms IS NOT NULL THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) AS with_errors
                FROM solana_consensus_jobs
            """).fetchone()
        result = {"active": 0, "processing": 0, "retry": 0}
        result.update({row["status"]: int(row["count"]) for row in rows})
        result["total"] = int(totals["total"] or 0)
        result["succeeded"] = int(totals["succeeded"] or 0)
        result["with_errors"] = int(totals["with_errors"] or 0)
        return result
