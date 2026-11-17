"""Append-only runtime telemetry stored in the existing generic events ledger.

Records are evidence of completed evaluations/provider calls, not proof that a
long-lived process is currently running. The dashboard derives freshness from
observed timestamps and never writes telemetry.
"""
from __future__ import annotations

import json
import time
from typing import Iterable, Any

from .database import connect_database, row_factory_for


AGENTS = ("scout", "whale", "sniper", "shield")


class RuntimeTelemetryStore:
    def __init__(self, database_path: str):
        if not isinstance(database_path, str) or not database_path.strip():
            raise ValueError("persistent database path is required")
        self.database_path = database_path
        self.initialize()

    def _connect(self):
        db = connect_database(self.database_path, timeout=10)
        db.row_factory = row_factory_for(self.database_path)
        return db

    def initialize(self) -> None:
        db = self._connect()
        try:
            db.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            db.commit()
        finally:
            db.close()

    def _append(self, event: str, payload: dict[str, Any], observed_at_ms: int) -> None:
        db = self._connect()
        try:
            db.execute(
                "INSERT INTO events(event,payload,created_at) VALUES(?,?,?)",
                (event, json.dumps(payload, sort_keys=True, separators=(",", ":")),
                 observed_at_ms / 1000),
            )
            db.commit()
        finally:
            db.close()

    def record_agent_votes(self, votes: Iterable[Any], *, observed_at_ms: int) -> None:
        for vote in votes:
            agent = str(getattr(vote, "agent", "")).strip().lower()
            if agent not in AGENTS:
                continue
            self._append("AGENT_HEARTBEAT", {
                "agent": agent,
                "status": "SUCCESS",
                "stance": str(getattr(vote, "stance", "UNKNOWN")),
                "observed_at_ms": observed_at_ms,
                "source": "solana_consensus_evaluation",
            }, observed_at_ms)

    def record_agent_failure(self, *, observed_at_ms: int, error_code: str) -> None:
        safe_code = error_code if error_code and error_code.replace("_", "").isalnum() else "AGENT_EVALUATION_ERROR"
        for agent in AGENTS:
            self._append("AGENT_HEARTBEAT", {
                "agent": agent,
                "status": "FAILED",
                "stance": None,
                "observed_at_ms": observed_at_ms,
                "error_code": safe_code[:64],
                "source": "solana_consensus_evaluation",
            }, observed_at_ms)

    def record_provider_probe(
        self, provider_id: str, chain: str, *, succeeded: bool,
        observed_at_ms: int, latency_ms: int, error_code: str | None = None,
    ) -> None:
        provider = "".join(ch for ch in str(provider_id).lower() if ch.isalnum() or ch in "-_.")[:80]
        if not provider:
            provider = "unknown-provider"
        safe_error = None
        if error_code:
            safe_error = "".join(ch for ch in str(error_code) if ch.isalnum() or ch in "_-")[:64] or "PROVIDER_ERROR"
        self._append("PROVIDER_PROBE", {
            "provider": provider,
            "chain": str(chain).lower()[:16],
            "status": "SUCCESS" if succeeded else "FAILED",
            "observed_at_ms": observed_at_ms,
            "latency_ms": max(0, int(latency_ms)),
            "error_code": safe_error,
        }, observed_at_ms)
