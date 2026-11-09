"""Append-only persistence for Solana liquidity reports and source inventories."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from ..liquidity_evidence import LiquidityEvidenceReport, LiquidityInventoryBatch


class SolanaLiquidityEvidenceStore:
    """Persist analyzed reports alongside the exact pool/locker inventory."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_liquidity_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_id TEXT NOT NULL UNIQUE,
                    mint TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    passed INTEGER NOT NULL,
                    pool_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_liquidity_mint_time "
                "ON solana_liquidity_reports(mint, observed_at_ms DESC, id DESC)"
            )

    def _connect(self):
        db = sqlite3.connect(self.database_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def record(
        self,
        report: LiquidityEvidenceReport,
        batch: LiquidityInventoryBatch | None = None,
    ) -> bool:
        if not isinstance(report, LiquidityEvidenceReport):
            raise TypeError("report must be a typed LiquidityEvidenceReport")
        if batch is not None:
            if not isinstance(batch, LiquidityInventoryBatch):
                raise TypeError("batch must be a typed LiquidityInventoryBatch")
            if report.token_address != batch.token_address or report.chain != batch.chain:
                raise ValueError("liquidity report and source inventory identity mismatch")
            if report.evidence_id == "":
                raise ValueError("liquidity report evidence identifier is required")
        payload = {
            "report": asdict(report),
            "inventory": asdict(batch) if batch is not None else None,
        }
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        evidence_id = f"solana-liquidity:{digest}"
        with self._connect() as db:
            cursor = db.execute("""
                INSERT OR IGNORE INTO solana_liquidity_reports
                    (evidence_id, mint, observed_at_ms, passed, pool_count, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                evidence_id, report.token_address, report.observed_at_ms,
                int(report.passed is True), report.pool_count, rendered,
            ))
            return cursor.rowcount == 1

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        item = dict(row)
        payload = json.loads(item.pop("payload_json"))
        # Keep the original report lookup shape while exposing raw inventory.
        item["report"] = payload.get("report", payload)
        item["inventory"] = payload.get("inventory")
        return item

    def latest(self, mint: str):
        with self._connect() as db:
            row = db.execute("""
                SELECT * FROM solana_liquidity_reports
                WHERE mint = ? ORDER BY observed_at_ms DESC, id DESC LIMIT 1
            """, (mint,)).fetchone()
        return self._decode(row)

    def history(self, mint: str, *, limit: int = 100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute("""
                SELECT * FROM solana_liquidity_reports
                WHERE mint = ? ORDER BY observed_at_ms DESC, id DESC LIMIT ?
            """, (mint, limit)).fetchall()
        return [self._decode(row) for row in rows]

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            if mint is None:
                row = db.execute("SELECT COUNT(*) FROM solana_liquidity_reports").fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_liquidity_reports WHERE mint = ?", (mint,)
                ).fetchone()
        return int(row[0])
