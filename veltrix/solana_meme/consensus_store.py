"""Append-only persistence for Phase 06 consensus decisions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from ..database import connect_database, ensure_database_parent, row_factory_for
from .consensus import ConsensusDecision


class ConsensusStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError("consensus decisions require a persistent SQLite database")
        ensure_database_parent(self.database_path)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_consensus_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mint TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    decision_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_solana_consensus_mint_time "
                "ON solana_consensus_decisions(mint, created_at_ms DESC, id DESC)"
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

    def record(self, decision: ConsensusDecision) -> bool:
        """Persist a consensus decision using a store-owned connection."""
        with self._connect() as db:
            return self.record_in_transaction(decision, db)

    def record_in_transaction(self, decision: ConsensusDecision, db: Any) -> bool:
        """Write a read-only decision on a caller-owned transaction connection."""
        if not isinstance(decision, ConsensusDecision):
            raise TypeError("decision must be a ConsensusDecision")
        if decision.status not in {"REJECTED", "WATCH", "ABSTAIN", "CONSENSUS_APPROVED"}:
            raise ValueError("unsupported consensus disposition")
        if decision.execution_authorized is not False:
            raise ValueError("Phase 06 consensus decisions cannot authorize execution")
        if decision.paper_review_eligible is not (decision.status == "CONSENSUS_APPROVED"):
            raise ValueError("paper-review eligibility must match the consensus disposition")
        if db is None or not callable(getattr(db, "execute", None)):
            raise TypeError("db must be an open database connection")
        payload = json.dumps(asdict(decision), sort_keys=True, separators=(",", ":"))
        cursor = db.execute(
            """INSERT OR IGNORE INTO solana_consensus_decisions
            (mint, created_at_ms, decision_id, payload_json)
            VALUES (?, ?, ?, ?)""",
            (decision.mint, decision.created_at_ms, decision.decision_id, payload),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> dict | None:
        return json.loads(row["payload_json"]) if row is not None else None

    def latest(self, mint: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT payload_json FROM solana_consensus_decisions
                WHERE mint = ? ORDER BY created_at_ms DESC, id DESC LIMIT 1""",
                (mint,),
            ).fetchone()
        return self._decode(row)

    def history(self, mint: str, *, limit: int = 100) -> list[dict]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload_json FROM solana_consensus_decisions
                WHERE mint = ? ORDER BY created_at_ms DESC, id DESC LIMIT ?""",
                (mint, limit),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def latest_all(self, *, limit: int = 100) -> list[dict]:
        """Return the latest persisted consensus decision for each mint."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT payload_json
                FROM (
                    SELECT payload_json, created_at_ms, id,
                           ROW_NUMBER() OVER (
                               PARTITION BY mint
                               ORDER BY created_at_ms DESC, id DESC
                           ) AS rank
                    FROM solana_consensus_decisions
                )
                WHERE rank = 1
                ORDER BY created_at_ms DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def recent(self, *, limit: int = 100) -> list[dict]:
        """Return recent persisted decisions across all mints, newest first."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as db:
            rows = db.execute(
                """SELECT payload_json FROM solana_consensus_decisions
                ORDER BY created_at_ms DESC, id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def count(self, mint: str | None = None) -> int:
        with self._connect() as db:
            if mint is None:
                row = db.execute("SELECT COUNT(*) FROM solana_consensus_decisions").fetchone()
            else:
                row = db.execute(
                    "SELECT COUNT(*) FROM solana_consensus_decisions WHERE mint = ?",
                    (mint,),
                ).fetchone()
        return int(row[0])

