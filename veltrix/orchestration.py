"""Durable, bounded, one-shot orchestration primitives.

This module is deliberately synchronous and backend-neutral. It does not create
background threads, timers, daemons, or hidden retry loops. Handlers must call
RunContext.checkpoint() at bounded work boundaries; a Python function already
blocked inside external I/O cannot be forcibly interrupted by this layer.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .database import connect_database, is_remote_database, row_factory_for


class LeaseBusy(RuntimeError):
    """Another worker currently owns the global execution lease."""


class RetryableJobError(RuntimeError):
    """A handler failure that should be retried by a later scheduled invocation."""


class JobDeadlineExceeded(TimeoutError):
    """The cooperative job deadline or work budget was exceeded."""


@dataclass
class RunContext:
    run_id: str
    idempotency_key: str
    attempt: int
    deadline_monotonic: float
    max_items: int
    _clock: Callable[[], float]
    _items: int = 0

    def checkpoint(self, *, items: int = 1) -> "RunContext":
        if isinstance(items, bool) or not isinstance(items, int) or items < 0:
            raise ValueError("items must be a non-negative integer")
        next_items = self._items + items
        if next_items > self.max_items:
            raise JobDeadlineExceeded("job item budget exceeded")
        if self._clock() >= self.deadline_monotonic:
            raise JobDeadlineExceeded("job deadline exceeded")
        self._items = next_items
        return self


class OrchestrationStore:
    """Durable run records, idempotency and a single global worker lease."""

    def __init__(self, database: str):
        if not isinstance(database, str) or not database.strip() or database == ":memory:":
            raise ValueError("a persistent database path or Turso URL is required")
        self.database = database

    def _connect(self):
        db = connect_database(self.database, timeout=10)
        db.row_factory = row_factory_for(self.database)
        if not is_remote_database(self.database):
            db.execute("PRAGMA foreign_keys=ON")
        return db

    def initialize(self) -> None:
        db = self._connect()
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS orchestration_jobs (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    job_type TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                      ('PENDING','RUNNING','RETRYABLE','SUCCEEDED','FAILED')),
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    next_attempt_at_ms INTEGER,
                    started_at_ms INTEGER,
                    finished_at_ms INTEGER,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_orchestration_jobs_due
                  ON orchestration_jobs(status,next_attempt_at_ms,created_at_ms);
                CREATE TABLE IF NOT EXISTS orchestration_worker_lease (
                    lease_name TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    acquired_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orchestration_audit (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL
                );
            """)
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _now(value: int | None) -> int:
        result = int(time.time() * 1000) if value is None else value
        if isinstance(result, bool) or not isinstance(result, int) or result <= 0:
            raise ValueError("timestamp must be a positive integer in milliseconds")
        return result

    @staticmethod
    def _audit(db, run_id: str | None, event_type: str, detail: dict, now: int):
        db.execute(
            "INSERT INTO orchestration_audit(event_id,run_id,event_type,detail_json,created_at_ms) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), run_id, event_type,
             json.dumps(detail, sort_keys=True, separators=(",", ":")), now),
        )

    def acquire_lease(self, owner_id: str, *, now_ms: int | None = None,
                      ttl_ms: int = 120_000, lease_name: str = "global-worker") -> int:
        now = self._now(now_ms)
        if not owner_id or not isinstance(owner_id, str) or not isinstance(lease_name, str) or not lease_name:
            raise ValueError("owner_id and lease_name are required")
        if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or not 1_000 <= ttl_ms <= 300_000:
            raise ValueError("ttl_ms must be between 1000 and 300000")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                INSERT INTO orchestration_worker_lease
                  (lease_name,owner_id,fencing_token,acquired_at_ms,expires_at_ms)
                VALUES(?,?,1,?,?)
                ON CONFLICT(lease_name) DO UPDATE SET
                  owner_id=excluded.owner_id,
                  fencing_token=orchestration_worker_lease.fencing_token+1,
                  acquired_at_ms=excluded.acquired_at_ms,
                  expires_at_ms=excluded.expires_at_ms
                WHERE orchestration_worker_lease.expires_at_ms <= ?
            """, (lease_name, owner_id, now, now + ttl_ms, now))
            row = db.execute(
                "SELECT owner_id,fencing_token,expires_at_ms FROM orchestration_worker_lease WHERE lease_name=?",
                (lease_name,),
            ).fetchone()
            if row is None or row["owner_id"] != owner_id or int(row["expires_at_ms"]) != now + ttl_ms:
                db.rollback()
                raise LeaseBusy("global worker lease is held by another active worker")
            db.commit()
            return int(row["fencing_token"])
        except Exception:
            if db:
                try:
                    db.rollback()
                except Exception:
                    pass
            raise
        finally:
            db.close()

    def renew_lease(self, owner_id: str, fencing_token: int, *,
                    now_ms: int | None = None, ttl_ms: int = 120_000,
                    lease_name: str = "global-worker") -> bool:
        now = self._now(now_ms)
        if isinstance(ttl_ms, bool) or not isinstance(ttl_ms, int) or not 1_000 <= ttl_ms <= 300_000:
            raise ValueError("ttl_ms must be between 1000 and 300000")
        if not owner_id or isinstance(fencing_token, bool) or not isinstance(fencing_token, int) or fencing_token < 1:
            raise ValueError("valid owner_id and fencing_token are required")
        db = self._connect()
        try:
            cursor = db.execute("""
                UPDATE orchestration_worker_lease SET expires_at_ms=?
                WHERE lease_name=? AND owner_id=? AND fencing_token=? AND expires_at_ms>?
            """, (now + ttl_ms, lease_name, owner_id, fencing_token, now))
            db.commit()
            return cursor.rowcount == 1
        finally:
            db.close()

    def release_lease(self, owner_id: str, fencing_token: int, *,
                      lease_name: str = "global-worker") -> bool:
        db = self._connect()
        try:
            cursor = db.execute(
                "DELETE FROM orchestration_worker_lease WHERE lease_name=? AND owner_id=? AND fencing_token=?",
                (lease_name, owner_id, fencing_token),
            )
            db.commit()
            return cursor.rowcount == 1
        finally:
            db.close()

    def enqueue(self, *, idempotency_key: str, job_type: str, payload: Any,
                max_attempts: int = 3, now_ms: int | None = None) -> dict:
        now = self._now(now_ms)
        if not idempotency_key or not job_type:
            raise ValueError("idempotency_key and job_type are required")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        try:
            canonical_payload = json.dumps(
                payload, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be finite, JSON-serializable data") from exc
        payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        run_id = str(uuid.uuid4())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                INSERT OR IGNORE INTO orchestration_jobs
                  (run_id,idempotency_key,job_type,payload_hash,status,max_attempts,created_at_ms,updated_at_ms)
                VALUES(?,?,?,?,'PENDING',?,?,?)
            """, (run_id, idempotency_key, job_type, payload_hash, max_attempts, now, now))
            row = db.execute(
                "SELECT * FROM orchestration_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row["payload_hash"] != payload_hash or row["job_type"] != job_type:
                raise ValueError("idempotency key was already used for a different job payload")
            self._audit(db, row["run_id"], "JOB_ENQUEUED",
                        {"status": row["status"], "duplicate": row["run_id"] != run_id}, now)
            db.commit()
            return dict(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def start_attempt(self, run_id: str, *, now_ms: int | None = None) -> dict | None:
        now = self._now(now_ms)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM orchestration_jobs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("unknown run_id")
            if row["status"] in {"SUCCEEDED", "FAILED", "RUNNING"}:
                db.commit()
                return None
            if row["status"] == "RETRYABLE" and row["next_attempt_at_ms"] is not None and int(row["next_attempt_at_ms"]) > now:
                db.commit()
                return None
            if int(row["attempt"]) >= int(row["max_attempts"]):
                db.execute("UPDATE orchestration_jobs SET status='FAILED',updated_at_ms=?,finished_at_ms=?,error_code='ATTEMPTS_EXHAUSTED' WHERE run_id=?",
                           (now, now, run_id))
                self._audit(db, run_id, "JOB_FAILED", {"error_code": "ATTEMPTS_EXHAUSTED"}, now)
                db.commit()
                return None
            attempt = int(row["attempt"]) + 1
            db.execute("""
                UPDATE orchestration_jobs SET status='RUNNING',attempt=?,started_at_ms=?,
                  updated_at_ms=?,next_attempt_at_ms=NULL,error_code=NULL WHERE run_id=?
            """, (attempt, now, now, run_id))
            self._audit(db, run_id, "ATTEMPT_STARTED", {"attempt": attempt}, now)
            db.commit()
            return {"run_id": run_id, "attempt": attempt, "max_attempts": int(row["max_attempts"])}
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def finish_attempt(self, run_id: str, attempt: int, *, outcome: str,
                       now_ms: int | None = None, retry_delay_ms: int = 30_000,
                       error_code: str | None = None) -> str:
        now = self._now(now_ms)
        if outcome not in {"SUCCEEDED", "RETRYABLE", "FAILED"}:
            raise ValueError("unsupported attempt outcome")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM orchestration_jobs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] != "RUNNING" or int(row["attempt"]) != attempt:
                raise ValueError("stale or invalid attempt completion")
            final = outcome
            next_at = None
            finished_at = now if outcome != "RETRYABLE" else None
            if outcome == "RETRYABLE":
                if attempt >= int(row["max_attempts"]):
                    final, finished_at = "FAILED", now
                    error_code = "ATTEMPTS_EXHAUSTED"
                else:
                    if isinstance(retry_delay_ms, bool) or not isinstance(retry_delay_ms, int) or not 1_000 <= retry_delay_ms <= 3_600_000:
                        raise ValueError("retry_delay_ms must be between 1000 and 3600000")
                    next_at = now + retry_delay_ms
            db.execute("""
                UPDATE orchestration_jobs SET status=?,updated_at_ms=?,next_attempt_at_ms=?,
                  finished_at_ms=?,error_code=? WHERE run_id=? AND attempt=? AND status='RUNNING'
            """, (final, now, next_at, finished_at, (error_code or "")[:80] or None, run_id, attempt))
            self._audit(db, run_id, "ATTEMPT_FINISHED",
                        {"attempt": attempt, "outcome": final, "error_code": error_code}, now)
            db.commit()
            return final
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def recover_interrupted(self, *, now_ms: int | None = None,
                            stale_after_ms: int = 300_000) -> int:
        """Mark old RUNNING attempts retryable; a later invocation performs retry."""
        now = self._now(now_ms)
        if isinstance(stale_after_ms, bool) or not isinstance(stale_after_ms, int) or not 1_000 <= stale_after_ms <= 86_400_000:
            raise ValueError("stale_after_ms must be between 1000 and 86400000")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT run_id,attempt,max_attempts FROM orchestration_jobs WHERE status='RUNNING' AND started_at_ms<=?",
                (now - stale_after_ms,),
            ).fetchall()
            for row in rows:
                final = "FAILED" if int(row["attempt"]) >= int(row["max_attempts"]) else "RETRYABLE"
                db.execute("""
                    UPDATE orchestration_jobs SET status=?,updated_at_ms=?,next_attempt_at_ms=?,
                      finished_at_ms=?,error_code='INTERRUPTED'
                    WHERE run_id=? AND status='RUNNING' AND attempt=?
                """, (final, now, now, now if final == "FAILED" else None,
                      row["run_id"], row["attempt"]))
                self._audit(db, row["run_id"], "ATTEMPT_RECOVERED",
                            {"attempt": int(row["attempt"]), "status": final}, now)
            db.commit()
            return len(rows)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get_job(self, run_id: str) -> dict | None:
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM orchestration_jobs WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None
        finally:
            db.close()


def run_once(store: OrchestrationStore, *, idempotency_key: str,
             job_type: str, payload: Any, handler: Callable[[RunContext, Any], Any],
             max_runtime_seconds: float = 45.0, max_items: int = 100,
             max_attempts: int = 3, retry_delay_ms: int = 30_000,
             lease_ttl_ms: int = 120_000,
             monotonic: Callable[[], float] = time.monotonic) -> dict:
    """Execute at most one attempt; retries are scheduled for later invocations."""
    if isinstance(max_runtime_seconds, bool) or not isinstance(max_runtime_seconds, (int, float)) or not math.isfinite(max_runtime_seconds) or not 1 <= max_runtime_seconds <= 240:
        raise ValueError("max_runtime_seconds must be between 1 and 240")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 10_000:
        raise ValueError("max_items must be between 1 and 10000")
    owner = str(uuid.uuid4())
    if isinstance(lease_ttl_ms, bool) or not isinstance(lease_ttl_ms, int) or lease_ttl_ms < int(max_runtime_seconds * 1000) + 15_000:
        raise ValueError("lease_ttl_ms must exceed the job deadline by at least 15 seconds")
    fencing = store.acquire_lease(owner, ttl_ms=lease_ttl_ms)
    try:
        store.recover_interrupted()
        job = store.enqueue(idempotency_key=idempotency_key, job_type=job_type,
                            payload=payload, max_attempts=max_attempts)
        attempt = store.start_attempt(job["run_id"])
        if attempt is None:
            return {"status": store.get_job(job["run_id"])["status"],
                    "run_id": job["run_id"], "executed": False}
        deadline = monotonic() + float(max_runtime_seconds)
        context = RunContext(job["run_id"], idempotency_key, attempt["attempt"],
                             deadline, max_items, monotonic)
        try:
            handler(context, payload)
            context.checkpoint(items=0)
            status = store.finish_attempt(job["run_id"], attempt["attempt"], outcome="SUCCEEDED")
        except RetryableJobError as exc:
            status = store.finish_attempt(job["run_id"], attempt["attempt"],
                outcome="RETRYABLE", retry_delay_ms=retry_delay_ms,
                error_code=type(exc).__name__)
        except Exception as exc:
            status = store.finish_attempt(job["run_id"], attempt["attempt"],
                outcome="FAILED", error_code=type(exc).__name__)
        return {"status": status, "run_id": job["run_id"],
                "attempt": attempt["attempt"], "executed": True}
    finally:
        store.release_lease(owner, fencing)
