"""Persistent candidate observations and supervised PumpPortal reconnects."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from contextlib import contextmanager
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator

from ..database import connect_database, ensure_database_parent, is_remote_database, row_factory_for
from .candidates import SolanaCandidate
from .pumpportal import PumpPortalDiscovery

LOGGER = logging.getLogger(__name__)


class LaunchStore:
    """SQLite store for candidate sightings, source provenance and feed outages."""

    def __init__(self, database_path: str | Path) -> None:
        supplied_path = str(database_path)
        self._uri = supplied_path == ":memory:"
        self.database_path = (
            f"file:veltrix_launch_{uuid.uuid4().hex}?mode=memory&cache=shared"
            if self._uri else supplied_path
        )
        self._keeper: sqlite3.Connection | None = None
        if self._uri:
            self._keeper = connect_database(self.database_path, uri=True, timeout=10)
        else:
            ensure_database_parent(self.database_path)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = connect_database(self.database_path, timeout=10, uri=self._uri)
        connection.row_factory = row_factory_for(self.database_path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            if not self._uri and not is_remote_database(self.database_path):
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
                    source TEXT NOT NULL DEFAULT 'pumpportal-new-token',
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER,
                    reason TEXT NOT NULL
                )
            """)
            columns = {
                row["name"] for row in db.execute(
                    "PRAGMA table_info(launch_feed_outages)"
                ).fetchall()
            }
            if "source" not in columns:
                db.execute(
                    "ALTER TABLE launch_feed_outages ADD COLUMN source TEXT "
                    "NOT NULL DEFAULT 'pumpportal-new-token'"
                )
            db.execute("""
                CREATE INDEX IF NOT EXISTS idx_launch_outages_active_source
                ON launch_feed_outages(source, ended_at_ms)
            """)

    @staticmethod
    def _timestamp(value: int | None, *, fallback_now: bool = False) -> int:
        if value is None and fallback_now:
            return int(time.time() * 1000)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("timestamp must be a positive integer")
        return value

    def record_candidate(self, candidate: SolanaCandidate) -> None:
        if not isinstance(candidate, SolanaCandidate):
            raise TypeError("only validated SolanaCandidate values can be stored")
        sources = sorted(set(candidate.sources or (candidate.source,)))
        with self._connect() as db:
            row = db.execute(
                "SELECT first_seen_at_ms, latest_seen_at_ms, primary_source, sources_json "
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
                is_newer = candidate.discovered_at_ms >= row["latest_seen_at_ms"]
                db.execute(
                    """UPDATE solana_launch_candidates
                    SET first_seen_at_ms = ?, latest_seen_at_ms = ?,
                        primary_source = ?, sources_json = ?
                    WHERE mint = ?""",
                    (
                        min(row["first_seen_at_ms"], candidate.discovered_at_ms),
                        max(row["latest_seen_at_ms"], candidate.discovered_at_ms),
                        candidate.source if is_newer else row["primary_source"],
                        json.dumps(merged_sources),
                        candidate.mint,
                    ),
                )
            db.execute(
                "INSERT INTO launch_feed_events(event_type, occurred_at_ms, details) "
                "VALUES (?, ?, ?)",
                (
                    "candidate_observed",
                    candidate.discovered_at_ms,
                    json.dumps({"mint": candidate.mint, "source": candidate.source}),
                ),
            )
            db.execute(
                "UPDATE launch_feed_outages SET ended_at_ms = ? "
                "WHERE source = ? AND ended_at_ms IS NULL",
                (candidate.discovered_at_ms, candidate.source),
            )

    def record_event(
        self,
        event_type: str,
        *,
        details: dict[str, Any] | None = None,
        occurred_at_ms: int | None = None,
    ) -> None:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type is required")
        timestamp = self._timestamp(occurred_at_ms, fallback_now=True)
        with self._connect() as db:
            db.execute(
                "INSERT INTO launch_feed_events(event_type, occurred_at_ms, details) "
                "VALUES (?, ?, ?)",
                (event_type.strip(), timestamp, json.dumps(details or {}, sort_keys=True)),
            )

    def record_outage(
        self,
        reason: str,
        *,
        source: str = "pumpportal-new-token",
        occurred_at_ms: int | None = None,
    ) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("outage reason is required")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("outage source is required")
        timestamp = self._timestamp(occurred_at_ms, fallback_now=True)
        clean_reason = reason.strip()[:500]
        clean_source = source.strip()
        with self._connect() as db:
            active = db.execute(
                "SELECT id FROM launch_feed_outages "
                "WHERE source = ? AND ended_at_ms IS NULL LIMIT 1",
                (clean_source,),
            ).fetchone()
            if active is None:
                db.execute(
                    "INSERT INTO launch_feed_outages(source, started_at_ms, reason) "
                    "VALUES (?, ?, ?)",
                    (clean_source, timestamp, clean_reason),
                )
            db.execute(
                "INSERT INTO launch_feed_events(event_type, occurred_at_ms, details) "
                "VALUES (?, ?, ?)",
                (
                    "feed_outage",
                    timestamp,
                    json.dumps({"source": clean_source, "reason": clean_reason}),
                ),
            )

    def record_source_healthy(
        self,
        source: str,
        *,
        occurred_at_ms: int | None = None,
    ) -> None:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source identity is required")
        timestamp = self._timestamp(occurred_at_ms, fallback_now=True)
        clean_source = source.strip()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE launch_feed_outages SET ended_at_ms = ? "
                "WHERE source = ? AND ended_at_ms IS NULL",
                (timestamp, clean_source),
            )
            if cursor.rowcount:
                db.execute(
                    "INSERT INTO launch_feed_events(event_type, occurred_at_ms, details) "
                    "VALUES (?, ?, ?)",
                    (
                        "feed_source_healthy",
                        timestamp,
                        json.dumps({"source": clean_source}),
                    ),
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

    def active_outage_count(self, *, source: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM launch_feed_outages WHERE ended_at_ms IS NULL"
        params: tuple[str, ...] = ()
        if source is not None:
            query += " AND source = ?"
            params = (source,)
        with self._connect() as db:
            return int(db.execute(query, params).fetchone()[0])

    def event_count(self, *, event_type: str | None = None) -> int:
        query = "SELECT COUNT(*) FROM launch_feed_events"
        params: tuple[str, ...] = ()
        if event_type is not None:
            query += " WHERE event_type = ?"
            params = (event_type,)
        with self._connect() as db:
            return int(db.execute(query, params).fetchone()[0])


class LaunchStreamManager:
    """Reconnect the primary launch feed with bounded exponential backoff."""

    def __init__(
        self,
        adapter: PumpPortalDiscovery,
        store: LaunchStore,
        *,
        initial_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 60.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            isinstance(initial_backoff_seconds, bool)
            or not isinstance(initial_backoff_seconds, (int, float))
            or not math.isfinite(initial_backoff_seconds)
            or initial_backoff_seconds <= 0
        ):
            raise ValueError("initial_backoff_seconds must be positive")
        if (
            isinstance(max_backoff_seconds, bool)
            or not isinstance(max_backoff_seconds, (int, float))
            or not math.isfinite(max_backoff_seconds)
            or max_backoff_seconds < initial_backoff_seconds
        ):
            raise ValueError("max_backoff_seconds must be >= initial_backoff_seconds")
        self.adapter = adapter
        self.store = store
        self.initial_backoff_seconds = float(initial_backoff_seconds)
        self.max_backoff_seconds = float(max_backoff_seconds)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def run(self, stop_event: threading.Event) -> None:
        """Run until stopped; persist every unexpected interruption as an outage."""
        backoff = self.initial_backoff_seconds
        while not stop_event.is_set():
            self.store.record_event(
                "feed_connection_attempt",
                details={"source": self.adapter.name},
                occurred_at_ms=self.clock_ms(),
            )
            saw_candidate = False
            try:
                for candidate in self.adapter.stream_new_tokens(stop_event=stop_event):
                    if stop_event.is_set():
                        break
                    self.store.record_candidate(candidate)
                    saw_candidate = True
                if stop_event.is_set():
                    break
                self.store.record_outage(
                    "stream ended unexpectedly",
                    source=self.adapter.name,
                    occurred_at_ms=self.clock_ms(),
                )
                LOGGER.warning("discovery stream ended unexpectedly: %s", self.adapter.name)
            except Exception as exc:
                if stop_event.is_set():
                    break
                self.store.record_outage(
                    f"{type(exc).__name__}: {exc}",
                    source=self.adapter.name,
                    occurred_at_ms=self.clock_ms(),
                )
                LOGGER.warning("discovery stream failed for %s: %s", self.adapter.name, exc)
            delay = self.initial_backoff_seconds if saw_candidate else backoff
            if stop_event.wait(delay):
                break
            backoff = (
                self.initial_backoff_seconds
                if saw_candidate else min(backoff * 2, self.max_backoff_seconds)
            )
