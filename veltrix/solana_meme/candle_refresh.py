"""Fenced, bounded ingestion of provider OHLCV into the persistent candle store."""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from ..database import connect_database, row_factory_for
from ..orchestration import LeaseBusy, RunContext
from .candle_data import SolanaCandle
from .candle_store import SolanaCandleStore
from .geckoterminal_candles import GeckoTerminalCandleProvider


@dataclass(frozen=True)
class CandleRefreshBatchResult:
    discovered: int
    processed: int
    fetched: int
    inserted: int
    duplicates: int
    failed: int
    deferred: int


class SolanaCandleRefreshWorker:
    """Refresh OHLCV for latest known market pools under global/per-item fences."""

    def __init__(
        self,
        database_path: str,
        candle_store: SolanaCandleStore | None = None,
        provider: GeckoTerminalCandleProvider | None = None,
        *,
        interval: str = "1m",
        refresh_interval_ms: int = 60_000,
        lease_ms: int = 120_000,
        retry_base_ms: int = 5_000,
        retry_max_ms: int = 300_000,
        limit_per_pool: int = 180,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if interval not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
            raise ValueError("unsupported candle interval")
        for name, value in (
            ("refresh_interval_ms", refresh_interval_ms), ("lease_ms", lease_ms),
            ("retry_base_ms", retry_base_ms), ("retry_max_ms", retry_max_ms),
            ("limit_per_pool", limit_per_pool),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if retry_max_ms < retry_base_ms or limit_per_pool > 1000:
            raise ValueError("invalid retry range or candle batch limit")
        self.database_path = str(database_path)
        self.store = candle_store or SolanaCandleStore(self.database_path)
        if self.store.database_path != self.database_path:
            raise ValueError("candle store must share the worker database")
        self.provider = provider or GeckoTerminalCandleProvider()
        self.interval = interval
        self.refresh_interval_ms = refresh_interval_ms
        self.lease_ms = lease_ms
        self.retry_base_ms = retry_base_ms
        self.retry_max_ms = retry_max_ms
        self.limit_per_pool = limit_per_pool
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
                CREATE TABLE IF NOT EXISTS solana_candle_refresh_jobs (
                    mint TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    pair_address TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','processing','retry')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    next_refresh_at_ms INTEGER NOT NULL,
                    lease_until_ms INTEGER,
                    claimed_by_owner_id TEXT,
                    claim_fencing_token INTEGER,
                    last_success_at_ms INTEGER,
                    last_error TEXT,
                    updated_at_ms INTEGER NOT NULL,
                    PRIMARY KEY(mint, interval)
                )
            """)
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_solana_candle_refresh_due
                ON solana_candle_refresh_jobs(status,next_refresh_at_ms,lease_until_ms)
            """)

    @staticmethod
    def _assert_claim(db: Any, mint: str, interval: str, pair: str,
                      now: int, context: RunContext) -> None:
        if not context.owner_id:
            raise LeaseBusy("run context has no lease owner identity")
        row = db.execute("""
            SELECT 1 FROM solana_candle_refresh_jobs
            WHERE mint=? AND interval=? AND pair_address=? AND status='processing'
              AND claimed_by_owner_id=? AND claim_fencing_token=? AND lease_until_ms>?
        """, (mint, interval, pair, context.owner_id, context.fencing_token, now)).fetchone()
        if row is None:
            raise LeaseBusy("candle refresh claim expired or was replaced")

    def _discover(self, now: int, context: RunContext | None) -> int:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
            # The latest persisted market snapshot is the source of truth for
            # the pool address; no mint-only or guessed pool is ever ingested.
            cursor = db.execute("""
                INSERT INTO solana_candle_refresh_jobs
                    (mint,interval,pair_address,status,next_refresh_at_ms,updated_at_ms)
                SELECT m.mint, ?, m.pair_address, 'active', ?, ?
                FROM solana_market_snapshots m
                JOIN (
                    SELECT mint, MAX(id) AS latest_id
                    FROM solana_market_snapshots GROUP BY mint
                ) latest ON latest.latest_id=m.id
                WHERE m.pair_address IS NOT NULL AND m.pair_address <> ''
                ON CONFLICT(mint,interval) DO UPDATE SET
                    pair_address=excluded.pair_address,
                    status=CASE WHEN solana_candle_refresh_jobs.pair_address <> excluded.pair_address
                                THEN 'active' ELSE solana_candle_refresh_jobs.status END,
                    next_refresh_at_ms=CASE WHEN solana_candle_refresh_jobs.pair_address <> excluded.pair_address
                                            THEN excluded.next_refresh_at_ms
                                            ELSE solana_candle_refresh_jobs.next_refresh_at_ms END,
                    updated_at_ms=excluded.updated_at_ms
            """, (self.interval, now, now))
            return cursor.rowcount

    def _claim(self, now: int, limit: int, context: RunContext | None) -> list[tuple[str,str]]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
            db.execute("""
                UPDATE solana_candle_refresh_jobs
                SET status='retry',lease_until_ms=NULL,claimed_by_owner_id=NULL,
                    claim_fencing_token=NULL,next_refresh_at_ms=?,updated_at_ms=?
                WHERE status='processing' AND lease_until_ms<=?
            """, (now, now, now))
            rows=db.execute("""
                SELECT mint,pair_address FROM solana_candle_refresh_jobs
                WHERE interval=? AND status IN ('active','retry') AND next_refresh_at_ms<=?
                ORDER BY next_refresh_at_ms,mint LIMIT ?
            """, (self.interval,now,limit)).fetchall()
            claimed=[]
            for row in rows:
                mint,pair=row["mint"],row["pair_address"]
                cursor=db.execute("""
                    UPDATE solana_candle_refresh_jobs SET status='processing',
                        attempts=attempts+1,lease_until_ms=?,updated_at_ms=?,
                        claimed_by_owner_id=?,claim_fencing_token=?
                    WHERE mint=? AND interval=? AND pair_address=? AND status IN ('active','retry')
                """, (now+self.lease_ms,now,context.owner_id if context else None,
                      context.fencing_token if context else None,mint,self.interval,pair))
                if cursor.rowcount == 1:
                    claimed.append((mint,pair))
            return claimed

    def _fail(self, mint: str, pair: str, error: Exception, now: int,
              context: RunContext | None) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if context is not None:
                context.assert_fence_in_transaction(db)
                self._assert_claim(db,mint,self.interval,pair,now,context)
            row=db.execute("""
                SELECT consecutive_failures FROM solana_candle_refresh_jobs
                WHERE mint=? AND interval=? AND pair_address=?
            """,(mint,self.interval,pair)).fetchone()
            failures=(int(row["consecutive_failures"]) if row else 0)+1
            delay=min(self.retry_base_ms*(2**min(failures-1,30)),self.retry_max_ms)
            cursor=db.execute("""
                UPDATE solana_candle_refresh_jobs SET status='retry',
                    consecutive_failures=?,next_refresh_at_ms=?,lease_until_ms=NULL,
                    claimed_by_owner_id=NULL,claim_fencing_token=NULL,last_error=?,updated_at_ms=?
                WHERE mint=? AND interval=? AND pair_address=? AND status='processing'
                  AND (? IS NULL OR (claimed_by_owner_id=? AND claim_fencing_token=?))
            """,(failures,now+delay,f"{type(error).__name__}: {error}"[:500],now,
                 mint,self.interval,pair,context.owner_id if context else None,
                 context.owner_id if context else None,context.fencing_token if context else None))
            if cursor.rowcount != 1:
                raise LeaseBusy("candle refresh failure update lost its row claim")

    def run_once(self, *, limit: int = 10,
                 context: RunContext | None = None) -> CandleRefreshBatchResult:
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=100:
            raise ValueError("limit must be between 1 and 100")
        now=self.clock_ms()
        if isinstance(now,bool) or not isinstance(now,int) or now<=0:
            raise ValueError("clock_ms must return a positive integer")
        discovered=self._discover(now,context)
        claimed=self._claim(now,limit,context)
        if context is not None and claimed:
            context.checkpoint(items=len(claimed))
        fetched=inserted=duplicates=failed=0
        for mint,pair in claimed:
            try:
                # Network I/O is deliberately outside the DB transaction.
                candles=self.provider.fetch_candles(mint,pair,self.interval,limit=self.limit_per_pool)
                if not isinstance(candles,(list,tuple)) or any(
                    not isinstance(c,SolanaCandle) or c.mint!=mint or c.pair_address!=pair
                    or c.interval!=self.interval for c in candles
                ):
                    raise ValueError("provider returned mismatched or invalid candle records")
                observed=self.clock_ms()
                if any(c.observed_at_ms>observed or observed-c.observed_at_ms>120_000 for c in candles):
                    raise ValueError("provider candles are stale or future-dated")
                with self._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if context is not None:
                        context.assert_fence_in_transaction(db)
                        self._assert_claim(db,mint,self.interval,pair,observed,context)
                    added=sum(1 for candle in candles if self.store.record_in_transaction(candle,db))
                    cursor=db.execute("""
                        UPDATE solana_candle_refresh_jobs SET status='active',
                            consecutive_failures=0,next_refresh_at_ms=?,lease_until_ms=NULL,
                            claimed_by_owner_id=NULL,claim_fencing_token=NULL,
                            last_success_at_ms=?,last_error=NULL,updated_at_ms=?
                        WHERE mint=? AND interval=? AND pair_address=? AND status='processing'
                          AND (? IS NULL OR (claimed_by_owner_id=? AND claim_fencing_token=?))
                    """,(observed+self.refresh_interval_ms,observed,observed,mint,self.interval,pair,
                         context.owner_id if context else None,context.owner_id if context else None,
                         context.fencing_token if context else None))
                    if cursor.rowcount!=1:
                        raise LeaseBusy("candle refresh completion lost its row claim")
                fetched+=len(candles); inserted+=added; duplicates+=len(candles)-added
            except LeaseBusy:
                raise
            except Exception as exc:
                self._fail(mint,pair,exc,self.clock_ms(),context)
                failed+=1
        with self._connect() as db:
            deferred=int(db.execute("""
                SELECT COUNT(*) FROM solana_candle_refresh_jobs
                WHERE interval=? AND status IN ('active','retry','processing')
            """,(self.interval,)).fetchone()[0])
        return CandleRefreshBatchResult(discovered,len(claimed),fetched,inserted,duplicates,failed,deferred)
