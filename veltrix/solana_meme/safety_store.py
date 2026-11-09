"""Append-only persistence for typed composite Solana safety certificates."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from ..safety import SafetyReport


class SolanaSafetyReportStore:
    """Store independently produced composite safety reports; never create them."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_composite_safety_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL UNIQUE,
                    mint TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    passed INTEGER NOT NULL,
                    composite_verified INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_safety_mint_time "
                "ON solana_composite_safety_reports(mint, observed_at_ms DESC, id DESC)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(self, report: SafetyReport) -> str:
        if not isinstance(report, SafetyReport):
            raise TypeError("report must be a typed SafetyReport")
        if not isinstance(report.token_address, str) or not report.token_address:
            raise ValueError("safety report mint is required")
        payload = json.dumps(asdict(report), sort_keys=True, separators=(",", ":"))
        evidence_id = "solana-composite-safety:" + hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()
        with self._connect() as db:
            db.execute("""
                INSERT OR IGNORE INTO solana_composite_safety_reports
                    (evidence_id, mint, observed_at_ms, passed,
                     composite_verified, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                evidence_id, report.token_address, report.observed_at_ms,
                int(report.passed is True), int(report.composite_verified is True), payload,
            ))
        return evidence_id

    def latest(self, mint: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("""
                SELECT * FROM solana_composite_safety_reports
                WHERE mint = ? ORDER BY observed_at_ms DESC, id DESC LIMIT 1
            """, (mint,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["report"] = json.loads(item.pop("payload_json"))
        return item

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            if mint is None:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_composite_safety_reports"
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_composite_safety_reports WHERE mint = ?",
                    (mint,),
                ).fetchone()
        return int(row[0])
