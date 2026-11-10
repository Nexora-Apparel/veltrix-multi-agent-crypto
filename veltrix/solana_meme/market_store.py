"""Append-only persistence for normalized Solana market intelligence."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..database import connect_database, ensure_database_parent, row_factory_for
from .market_data import SolanaMarketSnapshot


class MarketSnapshotStore:
    """Persist timestamped market observations without rewriting history."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError("market snapshots require a persistent SQLite database")
        ensure_database_parent(self.database_path)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mint TEXT NOT NULL,
                    pair_address TEXT NOT NULL,
                    symbol TEXT,
                    name TEXT,
                    price_usd REAL NOT NULL,
                    liquidity_usd REAL NOT NULL,
                    volume_m5_usd REAL,
                    volume_h1_usd REAL NOT NULL,
                    volume_h24_usd REAL,
                    price_change_m5_pct REAL,
                    price_change_h1_pct REAL,
                    price_change_h24_pct REAL,
                    txns_m5_buys INTEGER,
                    txns_m5_sells INTEGER,
                    txns_h1_buys INTEGER,
                    txns_h1_sells INTEGER,
                    txns_h24_buys INTEGER,
                    txns_h24_sells INTEGER,
                    fdv_usd REAL,
                    market_cap_usd REAL,
                    dex_id TEXT,
                    quote_token_symbol TEXT,
                    pool_token_side TEXT,
                    pair_count INTEGER NOT NULL,
                    retrieved_at_ms INTEGER NOT NULL,
                    response_received_at_ms INTEGER NOT NULL,
                    pair_created_at_ms INTEGER,
                    quality_flags_json TEXT NOT NULL,
                    UNIQUE(mint, pair_address, response_received_at_ms)
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_market_mint_time "
                "ON solana_market_snapshots(mint, retrieved_at_ms DESC, id DESC)"
            )

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

    def record(self, snapshot: SolanaMarketSnapshot) -> bool:
        if not isinstance(snapshot, SolanaMarketSnapshot):
            raise TypeError("snapshot must be a normalized SolanaMarketSnapshot")
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO solana_market_snapshots
                (mint, pair_address, symbol, name, price_usd, liquidity_usd,
                 volume_m5_usd, volume_h1_usd, volume_h24_usd,
                 price_change_m5_pct, price_change_h1_pct, price_change_h24_pct,
                 txns_m5_buys, txns_m5_sells, txns_h1_buys, txns_h1_sells,
                 txns_h24_buys, txns_h24_sells, fdv_usd, market_cap_usd,
                 dex_id, quote_token_symbol, pool_token_side, pair_count, retrieved_at_ms,
                 response_received_at_ms, pair_created_at_ms, quality_flags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.mint,
                    snapshot.selected_pair_address,
                    snapshot.symbol,
                    snapshot.name,
                    snapshot.price_usd,
                    snapshot.selected_pool_liquidity_usd,
                    snapshot.selected_pool_volume_m5_usd,
                    snapshot.selected_pool_volume_h1_usd,
                    snapshot.selected_pool_volume_h24_usd,
                    snapshot.price_change_m5_pct,
                    snapshot.price_change_h1_pct,
                    snapshot.price_change_h24_pct,
                    snapshot.txns_m5_buys,
                    snapshot.txns_m5_sells,
                    snapshot.txns_h1_buys,
                    snapshot.txns_h1_sells,
                    snapshot.txns_h24_buys,
                    snapshot.txns_h24_sells,
                    snapshot.fdv_usd,
                    snapshot.market_cap_usd,
                    snapshot.dex_id,
                    snapshot.quote_token_symbol,
                    snapshot.pool_token_side,
                    snapshot.pair_count,
                    snapshot.retrieved_at_ms,
                    snapshot.response_received_at_ms,
                    snapshot.pair_created_at_ms,
                    json.dumps(snapshot.quality_flags),
                ),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["quality_flags"] = json.loads(result.pop("quality_flags_json"))
        return result

    def latest(self, mint: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM solana_market_snapshots
                WHERE mint = ? ORDER BY retrieved_at_ms DESC, id DESC LIMIT 1""",
                (mint,),
            ).fetchone()
        return self._decode(row)

    def history(self, mint: str, *, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM solana_market_snapshots
                WHERE mint = ? ORDER BY retrieved_at_ms DESC, id DESC LIMIT ?""",
                (mint, limit),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            if mint is None:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_market_snapshots"
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_market_snapshots WHERE mint = ?",
                    (mint,),
                ).fetchone()
        return int(row[0])
