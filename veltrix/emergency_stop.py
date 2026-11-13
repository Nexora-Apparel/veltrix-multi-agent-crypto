"""Independent, persistent emergency-stop state for future execution gates.

The store starts in the stopped state. Clearing it requires an explicit
operator call. Read failures are propagated so callers must fail closed.
"""
from __future__ import annotations

from pathlib import Path
import re
import sqlite3
import time


class EmergencyStopError(RuntimeError):
    """Emergency-stop state is unavailable or invalid."""


class SQLiteEmergencyStop:
    def __init__(self, database_path: str | Path, *, timeout_seconds: float = 5.0):
        if not isinstance(database_path, (str, Path)):
            raise ValueError("database_path must be a filesystem path")
        path = str(database_path).strip()
        if not path or path == ":memory:":
            raise ValueError("emergency-stop state requires a persistent database")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.database_path = path
        self.timeout_seconds = float(timeout_seconds)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS execution_emergency_stop (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                    active INTEGER NOT NULL CHECK(active IN (0,1)),
                    reason_code TEXT NOT NULL,
                    changed_at_ms INTEGER NOT NULL
                )
            """)
            db.execute(
                """INSERT OR IGNORE INTO execution_emergency_stop
                   (singleton_id,active,reason_code,changed_at_ms)
                   VALUES (1,1,'uninitialized_fail_closed',?)""",
                (int(time.time() * 1000),),
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=self.timeout_seconds)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _reason(reason_code: str) -> str:
        if not isinstance(reason_code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", reason_code):
            raise ValueError("reason_code must be a lowercase code")
        return reason_code

    def set_active(
        self,
        active: bool,
        *,
        reason_code: str,
        changed_at_ms: int | None = None,
    ) -> None:
        if not isinstance(active, bool):
            raise ValueError("active must be boolean")
        reason = self._reason(reason_code)
        stamp = int(time.time() * 1000) if changed_at_ms is None else changed_at_ms
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp <= 0:
            raise ValueError("changed_at_ms must be a positive integer")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                """UPDATE execution_emergency_stop
                   SET active=?,reason_code=?,changed_at_ms=? WHERE singleton_id=1""",
                (1 if active else 0, reason, stamp),
            )
            if cursor.rowcount != 1:
                db.execute("ROLLBACK")
                raise EmergencyStopError("emergency-stop state row is missing")
            db.execute("COMMIT")

    def status(self) -> dict:
        try:
            with self._connect() as db:
                row = db.execute(
                    """SELECT active,reason_code,changed_at_ms
                       FROM execution_emergency_stop WHERE singleton_id=1"""
                ).fetchone()
        except sqlite3.Error as exc:
            raise EmergencyStopError("emergency-stop storage is unavailable") from exc
        if row is None or row["active"] not in (0, 1):
            raise EmergencyStopError("emergency-stop state is missing or malformed")
        return {
            "active": bool(row["active"]),
            "reason_code": row["reason_code"],
            "changed_at_ms": row["changed_at_ms"],
        }

    def is_active(self) -> bool:
        return self.status()["active"]
