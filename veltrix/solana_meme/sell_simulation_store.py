"""Append-only persistence for controlled unsigned sell simulations."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from ..providers.solana_sell_simulation import SolanaSellSimulationObservation


class SolanaSellSimulationStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_sell_simulation_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL UNIQUE,
                    mint TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    simulated_slot INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_sell_sim_mint_time "
                "ON solana_sell_simulation_reports(mint, observed_at_ms DESC, id DESC)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(self, observation: SolanaSellSimulationObservation) -> bool:
        if not isinstance(observation, SolanaSellSimulationObservation):
            raise TypeError("observation must be a typed SolanaSellSimulationObservation")
        if observation.transaction_submitted or observation.signature_verification:
            raise ValueError("simulation evidence must be unsigned and unsubmitted")
        payload = json.dumps(asdict(observation), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_sell_simulation_reports
                    (evidence_id, mint, observed_at_ms, simulated_slot, state, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                f"solana-sell-simulation:{digest}", observation.mint,
                observation.observed_at_ms, observation.simulated_slot,
                observation.state, payload,
            ))
            return cursor.rowcount == 1

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        item = dict(row)
        item["report"] = json.loads(item.pop("payload_json"))
        return item

    def latest(self, mint: str):
        with self._connect() as db:
            row = db.execute("""
                SELECT * FROM solana_sell_simulation_reports
                WHERE mint=? ORDER BY observed_at_ms DESC, id DESC LIMIT 1
            """, (mint,)).fetchone()
        return self._decode(row)

    def history(self, mint: str, *, limit: int = 100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute("""
                SELECT * FROM solana_sell_simulation_reports
                WHERE mint=? ORDER BY observed_at_ms DESC, id DESC LIMIT ?
            """, (mint, limit)).fetchall()
        return [self._decode(row) for row in rows]

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            if mint is None:
                row = db.execute("SELECT COUNT(*) FROM solana_sell_simulation_reports").fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_sell_simulation_reports WHERE mint=?", (mint,)
                ).fetchone()
        return int(row[0])
