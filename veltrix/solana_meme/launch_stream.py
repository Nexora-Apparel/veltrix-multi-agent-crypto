"""Supervise a read-only Solana launch feed and persist its coverage.

This module stores observations and stream interruptions only. It has no
wallet, transaction, swap, or signing capability.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .candidates import SolanaCandidate
from .pumpportal import PumpPortalDiscovery


class LaunchStore:
    """Durable SQLite store for candidate observations and feed coverage."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS solana_launch_candidates (
                    mint TEXT PRIMARY KEY,
                    first_seen_at_ms INTEGER NOT NULL,
                    latest_seen_at_ms INTEGER NOT NULL,
                    primary_source TEXT NOT NULL,
                    sources_json TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS launch_feed_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    occurred_at_ms INTEGER NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}'
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS launch_feed_outages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER,
                    reason TEXT NOT NULL
                )
            """)

    def record_candidate(self, candidate: SolanaCandidate) -> None:
        if not isinstance(candidate, SolanaCandidate):
            raise TypeError("only normalized SolanaCandidate values can be stored")
        sources = sorted(set(candidate.sources or (candidate.source,)))
        with self._connect() as db:
            row = db.execute(
                "SELECT first_seen_at_ms, latest_seen_at_ms, sources_json "
                "FROM solana_launch_candidates WHERE mint = ?",
                (candidate.mint,),
            ).fetchone()
            if row is None:
                db.execute(
                    """INSERT INTO solana_launch_candidates
                    (mint, first_seen_at_ms, latest_seen_at_ms, primary_source, sources_json)
                    VALUES (?, ?, ?, ?, ?)""",
                    (candidate.mint, candidate.discovered_at_ms,
                     candidate.discovered_at_ms, candidate.source, json.dumps(sources)),
                )
            else:
                merged_sources = sorted(set(json.loads(row["sources_json"])) | set(sources))
                db.execute(
                    """UPDATE solana_launch_candidates
                    SET latest_seen_at_ms = ?, primary_source = ?, sources_json = ?
                    WHERE mint = ?""",
                    (max(row["latest_seen_at_ms"], candidate.discovered_at_ms),
                     candidate.source, json.dumps(merged_sources), candidate.mint),
                )
            db.execute(
                "INSERT INTO launch_feed_events(event_type, occurred_at_ms, details) "
                "VALUES (?, ?, ?)",
                ("candidate_observed", candidate.discovered_at_ms,
                 json.dumps({"mint": candidate.mint, "source": candidate.source})),
            )
            db.execute(
                "UPDATE launch_feed_outages SET ended_at_ms = ? "
                "WHERE ended_at_ms IS NULL",
                (candidate.discovered_at_ms,),
            )

    def record_event(self, event_type: str, *, details: dict[str, Any] | None = None,
                     occurred_at_ms: int | None = None) -> None:
        if not event_type or not isinstance(event_type, str):
            raise ValueError("event_type is required")
        timestamp = occurred_at_ms or int(time.time() * 1000)
        with self._connect() as db:
            db.execute(
                "INSERT INTO launch_feed_events(event_type, occurred_at_ms, details) "
                "VALUES (?, ?, ?)",
                (event_type, timestamp, json.dumps(details or {}, sort_keys=True)),
            )

    def record_outage(self, reason: str, *, occurred_at_ms: int | None = None) -> None:
        timestamp = occurred_at_ms or int(time.time() * 1000)
        with self._connect() as db:
            active = db.execute(
                "SELECT id FROM launch_feed_outages WHERE ended_at_ms IS NULL LIMIT 1"
            ).fetchone()
            if active is None:
                db.execute(
                    "INSERT INTO launch_feed_outages(started_at_ms, reason) VALUES (?, ?)",
                    (timestamp, str(reason)[:500]),
                )
            db.execute(
                "INSERT INTO launch_feed_events(event_type, occurred_at_ms, details) "
                "VALUES (?, ?, ?)",
                ("feed_outage", timestamp, json.dumps({"reason": str(reason)[:500]})),
            )

    def list_candidates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as db:
            rows = db.execute("""
                SELECT mint, first_seen_at_ms, latest_seen_at_ms, primary_source, sources_json
                FROM solana_launch_candidates
                ORDER BY latest_seen_at_ms DESC, mint ASC LIMIT ?
            """, (limit,)).fetchall()
        return [
            {
                "mint": row["mint"],
                "first_seen_at_ms": row["first_seen_at_ms"],
                "latest_seen_at_ms": row["latest_seen_at_ms"],
                "primary_source": row["primary_source"],
                "sources": json.loads(row["sources_json"]),
            }
            for row in rows
        ]

    def candidate_count(self) -> int:
        with self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM solana_launch_candidates"
            ).fetchone()[0])

    def active_outage_count(self) -> int:
        with self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM launch_feed_outages WHERE ended_at_ms IS NULL"
            ).fetchone()[0])


class LaunchStreamManager:
    """Reconnect a discovery adapter with bounded exponential backoff."""

    def __init__(
        self,
        adapter: PumpPortalDiscovery,
        store: LaunchStore,
        *,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if initial_backoff_seconds <= 0:
            raise ValueError("initial_backoff_seconds must be positive")
        if max_backoff_seconds < initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= initial_backoff_seconds")
        self.adapter = adapter
        self.store = store
        self.initial_backoff_seconds = initial_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def run(self, stop_event: threading.Event) -> None:
        """Run until stopped; every interruption is persisted as a coverage gap."""
        backoff = self.initial_backoff_seconds
        while not stop_event.is_set():
            self.store.record_event("feed_connection_attempt", occurred_at_ms=self.clock_ms())
            try:
                saw_candidate = False
                for candidate in self.adapter.stream_new_tokens(stop_event=stop_event):
                    if stop_event.is_set():
                        break
                    self.store.record_candidate(candidate)
                    saw_candidate = True
                if stop_event.is_set():
                    break
                reason = "stream ended without stop request"
                self.store.record_outage(reason, occurred_at_ms=self.clock_ms())
            except Exception as exc:
                if stop_event.is_set():
                    break
                self.store.record_outage(
                    f"{type(exc).__name__}: {exc}", occurred_at_ms=self.clock_ms()
                )
            if stop_event.wait(backoff):
                break
            if saw_candidate:
                backoff = self.initial_backoff_seconds
            else:
                backoff = min(backoff * 2, self.max_backoff_seconds)
