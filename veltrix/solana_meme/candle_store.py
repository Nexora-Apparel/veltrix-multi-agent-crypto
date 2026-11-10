"""Append-only OHLCV persistence with provider provenance."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Iterator

from ..database import connect_database, ensure_database_parent, row_factory_for
from .candle_data import INTERVAL_SECONDS, SolanaCandle


class SolanaCandleStore:
    def __init__(self, database_path: str) -> None:
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError("candles require a persistent database")
        ensure_database_parent(self.database_path)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_ohlcv_candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mint TEXT NOT NULL,
                    pair_address TEXT NOT NULL,
                    interval TEXT NOT NULL,
                    open_time_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    source TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    is_closed INTEGER NOT NULL CHECK(is_closed IN (0,1)),
                    UNIQUE(mint,pair_address,interval,open_time_ms,source)
                )
            """)
            db.execute("""CREATE INDEX IF NOT EXISTS idx_solana_candles_lookup
                ON solana_ohlcv_candles(mint,pair_address,interval,open_time_ms DESC)""")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
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

    def record(self, candle: SolanaCandle) -> bool:
        if not isinstance(candle, SolanaCandle):
            raise TypeError("candle must be a validated SolanaCandle")
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_ohlcv_candles
                (mint,pair_address,interval,open_time_ms,open,high,low,close,
                 volume,source,observed_at_ms,is_closed)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                candle.mint,candle.pair_address,candle.interval,candle.open_time_ms,
                candle.open,candle.high,candle.low,candle.close,candle.volume,
                candle.source,candle.observed_at_ms,int(candle.is_closed),
            ))
            return cursor.rowcount == 1

    def history(self, mint: str, pair_address: str, interval: str, *, limit: int = 500) -> list[dict]:
        if interval not in INTERVAL_SECONDS:
            raise ValueError("unsupported candle interval")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("limit must be between 1 and 5000")
        with self._connect() as db:
            rows = db.execute("""
                SELECT mint,pair_address,interval,open_time_ms,open,high,low,close,
                       volume,source,observed_at_ms,is_closed
                FROM solana_ohlcv_candles
                WHERE mint=? AND pair_address=? AND interval=?
                ORDER BY open_time_ms DESC LIMIT ?
            """, (mint,pair_address,interval,limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM solana_ohlcv_candles" if mint is None
                else "SELECT COUNT(*) FROM solana_ohlcv_candles WHERE mint=?",
                () if mint is None else (mint,),
            ).fetchone()
        return int(row[0])
