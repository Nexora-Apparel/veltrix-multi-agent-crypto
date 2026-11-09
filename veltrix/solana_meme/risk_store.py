"""Append-only storage for Solana risk evidence reports."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from .risk_evidence import SolanaRiskEvidenceReport


class SolanaRiskEvidenceStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError("risk evidence requires a persistent SQLite database")
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_risk_evidence_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mint TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    evidence_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(mint, evidence_hash)
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_risk_mint_time "
                "ON solana_risk_evidence_reports(mint, observed_at_ms DESC, id DESC)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def record(self, report: SolanaRiskEvidenceReport) -> bool:
        if not isinstance(report, SolanaRiskEvidenceReport):
            raise TypeError("report must be a SolanaRiskEvidenceReport")
        payload_data = asdict(report)
        payload_data["risk_state"] = report.risk_state
        payload = json.dumps(payload_data, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO solana_risk_evidence_reports
                (mint, observed_at_ms, evidence_hash, payload_json)
                VALUES (?, ?, ?, ?)""",
                (report.mint, report.observed_at_ms, digest, payload),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        return json.loads(row["payload_json"]) if row is not None else None

    def latest(self, mint: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT payload_json FROM solana_risk_evidence_reports
                WHERE mint = ? ORDER BY observed_at_ms DESC, id DESC LIMIT 1""",
                (mint,),
            ).fetchone()
        return self._decode(row)

    def history(self, mint: str, *, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload_json FROM solana_risk_evidence_reports
                WHERE mint = ? ORDER BY observed_at_ms DESC, id DESC LIMIT ?""",
                (mint, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            if mint is None:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_risk_evidence_reports"
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_risk_evidence_reports WHERE mint = ?",
                    (mint,),
                ).fetchone()
        return int(row[0])
