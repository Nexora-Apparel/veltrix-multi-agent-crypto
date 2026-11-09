"""Append-only persistence for Solana sell-route evidence observations."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .sellability import SolanaSellabilityEvidence


class SolanaSellabilityEvidenceStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_sellability_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL UNIQUE,
                    mint TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_sellability_mint_time "
                "ON solana_sellability_reports(mint, observed_at_ms DESC, id DESC)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(self, report: SolanaSellabilityEvidence) -> bool:
        if not isinstance(report, SolanaSellabilityEvidence):
            raise TypeError("report must be a typed SolanaSellabilityEvidence")
        payload = json.dumps(asdict(report), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        evidence_id = f"solana-sellability:{digest}"
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_sellability_reports
                    (evidence_id, mint, observed_at_ms, state, payload_json)
                VALUES (?, ?, ?, ?, ?)
            """, (evidence_id, report.mint, report.observed_at_ms, report.state, payload))
            return cursor.rowcount == 1

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["report"] = json.loads(result.pop("payload_json"))
        return result

    def latest(self, mint: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("""
                SELECT * FROM solana_sellability_reports
                WHERE mint = ? ORDER BY observed_at_ms DESC, id DESC LIMIT 1
            """, (mint,)).fetchone()
        return self._decode(row)

    def history(self, mint: str, *, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute("""
                SELECT * FROM solana_sellability_reports
                WHERE mint = ? ORDER BY observed_at_ms DESC, id DESC LIMIT ?
            """, (mint, limit)).fetchall()
        return [self._decode(row) for row in rows]

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            if mint is None:
                row = db.execute("SELECT COUNT(*) FROM solana_sellability_reports").fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_sellability_reports WHERE mint = ?",
                    (mint,),
                ).fetchone()
        return int(row[0])
