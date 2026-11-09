"""Append-only persistence for finalized simulation-wallet preflight reads."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from ..providers.solana_simulation_preflight import SolanaSimulationWalletPreflight


class SolanaSimulationPreflightStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_simulation_preflights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL UNIQUE,
                    mint TEXT NOT NULL,
                    wallet TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_sim_preflight_mint_time "
                "ON solana_simulation_preflights(mint, observed_at_ms DESC, id DESC)"
            )

    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(self, observation: SolanaSimulationWalletPreflight) -> bool:
        if not isinstance(observation, SolanaSimulationWalletPreflight):
            raise TypeError("observation must be a typed preflight result")
        payload = json.dumps(asdict(observation), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_simulation_preflights
                    (evidence_id, mint, wallet, observed_at_ms, state, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                f"solana-simulation-preflight:{digest}", observation.mint,
                observation.wallet, observation.observed_at_ms,
                observation.state, payload,
            ))
            return cursor.rowcount == 1

    def latest(self, mint: str):
        with self._connect() as db:
            row = db.execute("""
                SELECT * FROM solana_simulation_preflights
                WHERE mint=? ORDER BY observed_at_ms DESC, id DESC LIMIT 1
            """, (mint,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["report"] = json.loads(result.pop("payload_json"))
        return result

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            if mint is None:
                row = db.execute("SELECT COUNT(*) FROM solana_simulation_preflights").fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_simulation_preflights WHERE mint=?", (mint,)
                ).fetchone()
        return int(row[0])
