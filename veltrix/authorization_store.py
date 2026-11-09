"""Durable, atomic storage for one-use execution authorizations.

This store persists authorization IDs only; it never stores signing keys,
wallet secrets, signatures, or transaction material.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import sqlite3
import time


@dataclass(frozen=True, slots=True)
class SQLiteAuthorizationConsumer:
    """Consume each authorization ID at most once across threads and restarts.

    A dedicated SQLite connection is opened for each consume operation.
    BEGIN IMMEDIATE serializes competing writers, while the primary key is the
    durable replay barrier. Rows are deliberately retained after expiry:
    deleting them would permit an old signed authorization to be replayed.
    """

    database_path: str | Path
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.database_path, (str, Path)):
            raise ValueError("database_path must be a filesystem path")
        path = str(self.database_path)
        if not path or path == ":memory:":
            raise ValueError("database_path must be a persistent filesystem path")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds must be a positive number")
        if not math.isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(path),
            timeout=float(self.timeout_seconds),
            isolation_level=None,
        )
        try:
            busy_timeout_ms = max(1, int(float(self.timeout_seconds) * 1000))
            connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS consumed_authorizations (
                    authorization_id TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL,
                    consumed_at INTEGER NOT NULL
                )
                """
            )
            return connection
        except Exception:
            connection.close()
            raise

    def consume(self, authorization_id: str, expires_at: int) -> bool:
        """Atomically persist an authorization ID; return false on replay."""
        if not isinstance(authorization_id, str) or not authorization_id:
            raise ValueError("authorization_id must be a non-empty string")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0:
            raise ValueError("expires_at must be a positive integer timestamp")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO consumed_authorizations
                        (authorization_id, expires_at, consumed_at)
                    VALUES (?, ?, ?)
                    """,
                    (authorization_id, expires_at, int(time.time())),
                )
            except sqlite3.IntegrityError:
                connection.execute("ROLLBACK")
                return False
            connection.execute("COMMIT")
            return True
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
