"""Append-only persistence for normalized Solana market observations."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .market_data import SolanaMarketSnapshot


class MarketSnapshotStore:
    """Persist timestamped snapshots without overwriting prior market evidence."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mint TEXT NOT NULL,
                    pair_address TEXT NOT NULL,
                    symbol TEXT,
                    price_usd REAL NOT NULL,
                    liquidity_usd REAL NOT NULL,
                    volume_h1_usd REAL NOT NULL,
                    price_change_m5_pct REAL NOT NULL,
                    pair_count INTEGER NOT NULL,
                    retrieved_at_ms INTEGER NOT NULL,
                    provider_observed_at_ms INTEGER NOT NULL,
                    pair_created_at_ms INTEGER,
                    quality_flags_json TEXT NOT NULL,
                    UNIQUE(mint, pair_address, provider_observed_at_ms)
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_market_mint_time "
                "ON solana_market_snapshots(mint, retrieved_at_ms DESC)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(self, snapshot: SolanaMarketSnapshot) -> bool:
        if not isinstance(snapshot, SolanaMarketSnapshot):
            raise TypeError("snapshot must be a normalized SolanaMarketSnapshot")
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO solana_market_snapshots
                (mint, pair_address, symbol, price_usd, liquidity_usd, volume_h1_usd,
                 price_change_m5_pct, pair_count, retrieved_at_ms,
                 provider_observed_at_ms, pair_created_at_ms, quality_flags_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.mint, snapshot.selected_pair_address, snapshot.symbol,
                    snapshot.price_usd, snapshot.selected_pool_liquidity_usd,
                    snapshot.selected_pool_volume_h1_usd, snapshot.price_change_m5_pct,
                    snapshot.pair_count, snapshot.retrieved_at_ms,
                    snapshot.provider_observed_at_ms, snapshot.pair_created_at_ms,
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
        if isinstance(limit, bool) or limit < 1 or limit > 10_000:
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
