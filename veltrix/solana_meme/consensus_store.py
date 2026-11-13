"""Append-only persistence for Phase 06 consensus decisions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from .consensus import AgentVote, ConsensusDecision, validate_consensus_decision
from .risk_evidence import RiskFlag


class ConsensusStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path == ":memory:":
            raise ValueError("consensus decisions require a persistent SQLite database")
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
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

    def record(self, decision: ConsensusDecision) -> bool:
        decision = validate_consensus_decision(decision)
        payload = json.dumps(asdict(decision), sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO solana_consensus_decisions
                (mint, created_at_ms, decision_id, payload_json)
                VALUES (?, ?, ?, ?)""",
                (decision.mint, decision.created_at_ms, decision.decision_id, payload),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _decode_payload(payload_json: str) -> dict:
        try:
            payload = json.loads(payload_json)
            if type(payload) is not dict:
                raise ValueError("persisted consensus payload must be an object")
            payload["votes"] = tuple(
                AgentVote(
                    agent=vote["agent"],
                    stance=vote["stance"],
                    reason=vote["reason"],
                    evidence_refs=tuple(vote["evidence_refs"]),
                    observed_at_ms=vote["observed_at_ms"],
                )
                for vote in payload["votes"]
            )
            payload["risk_flags"] = tuple(RiskFlag(**flag) for flag in payload["risk_flags"])
            for name in ("supporting_agents", "missing_claims", "reasons"):
                payload[name] = tuple(payload[name])
            checked = validate_consensus_decision(ConsensusDecision(**payload))
            return json.loads(json.dumps(asdict(checked), sort_keys=True, separators=(",", ":")))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("persisted consensus payload failed canonical validation") from None

    @classmethod
    def _decode(cls, row: sqlite3.Row | None) -> dict | None:
        return cls._decode_payload(row["payload_json"]) if row is not None else None

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
        return [self._decode_payload(row["payload_json"]) for row in rows]

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
        return [self._decode_payload(row["payload_json"]) for row in rows]

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

