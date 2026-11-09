"""Append-only persistence for claim-separated Solana risk evidence reports."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from .risk_evidence import SolanaRiskEvidenceReport


class SolanaRiskEvidenceStore:
    """Persist full evidence reports without overwriting prior observations."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_risk_evidence_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL UNIQUE,
                    mint TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    risk_state TEXT NOT NULL,
                    missing_claims_json TEXT NOT NULL,
                    errors_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_risk_mint_time "
                "ON solana_risk_evidence_reports(mint, observed_at_ms DESC, id DESC)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(self, report: SolanaRiskEvidenceReport) -> bool:
        if not isinstance(report, SolanaRiskEvidenceReport):
            raise TypeError("report must be a typed SolanaRiskEvidenceReport")
        payload = json.dumps(asdict(report), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        evidence_id = f"solana-risk:{digest}"
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_risk_evidence_reports
                    (evidence_id, mint, observed_at_ms, risk_state,
                     missing_claims_json, errors_json, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                evidence_id, report.mint, report.observed_at_ms, report.risk_state,
                json.dumps(report.missing_claims), json.dumps(report.errors), payload,
            ))
            return cursor.rowcount == 1

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["missing_claims"] = json.loads(result.pop("missing_claims_json"))
        result["errors"] = json.loads(result.pop("errors_json"))
        result["report"] = json.loads(result.pop("payload_json"))
        return result

    def latest(self, mint: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("""
                SELECT * FROM solana_risk_evidence_reports
                WHERE mint = ? ORDER BY observed_at_ms DESC, id DESC LIMIT 1
            """, (mint,)).fetchone()
        return self._decode(row)

    def history(self, mint: str, *, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute("""
                SELECT * FROM solana_risk_evidence_reports
                WHERE mint = ? ORDER BY observed_at_ms DESC, id DESC LIMIT ?
            """, (mint, limit)).fetchall()
        return [self._decode(row) for row in rows]

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            if mint is None:
                row = db.execute("SELECT COUNT(*) FROM solana_risk_evidence_reports").fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_risk_evidence_reports WHERE mint = ?",
                    (mint,),
                ).fetchone()
        return int(row[0])
