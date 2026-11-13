"""Durable execution-intent state and append-only, redacted audit records.

The coordinator deliberately stops at AUTHORIZED. SIGNED, SUBMITTED and
CONFIRMED transitions are disabled in this release; no signer or submitter is
accepted by this module.
"""
from __future__ import annotations

from enum import Enum
import hashlib
import hmac
import json
from pathlib import Path
from contextlib import closing
import re
import sqlite3
import time
import uuid


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ExecutionDisabled(RuntimeError):
    """A transition would enter an unavailable live-execution state."""


class IdempotencyConflict(RuntimeError):
    """An intent ID was reused with different immutable intent scope."""


class AuditIntegrityError(RuntimeError):
    """The append-only audit hash chain is broken or has been altered."""


class ExecutionState(str, Enum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SIMULATED = "SIMULATED"
    AUTHORIZED = "AUTHORIZED"
    SIGNED = "SIGNED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_TERMINAL = {ExecutionState.CONFIRMED, ExecutionState.FAILED, ExecutionState.CANCELLED}
_DISABLED = {ExecutionState.SIGNED, ExecutionState.SUBMITTED, ExecutionState.CONFIRMED}
_ALLOWED = {
    ExecutionState.CREATED: {ExecutionState.VALIDATED, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.VALIDATED: {ExecutionState.SIMULATED, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.SIMULATED: {ExecutionState.AUTHORIZED, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.AUTHORIZED: {ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.SIGNED: {ExecutionState.SUBMITTED, ExecutionState.FAILED},
    ExecutionState.SUBMITTED: {ExecutionState.CONFIRMED, ExecutionState.FAILED},
    ExecutionState.CONFIRMED: set(),
    ExecutionState.FAILED: set(),
    ExecutionState.CANCELLED: set(),
}


def _safe_id(value: str, name: str = "intent_id") -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError(f"{name} is malformed")
    return value


def _safe_code(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CODE_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase code")
    return value


def _safe_hash(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _stamp(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("timestamp must be a positive integer")
    return value


class ExecutionAuditStore:
    """Append-only audit view; only allowlisted metadata can be persisted."""

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        if not self.database_path or self.database_path == ":memory:":
            raise ValueError("execution audit requires a persistent database")
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            self._ensure_schema(db)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _ensure_schema(db: sqlite3.Connection) -> None:
        db.execute("""
            CREATE TABLE IF NOT EXISTS execution_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                intent_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                state TEXT NOT NULL,
                reason_code TEXT,
                policy_hash TEXT,
                evidence_hash TEXT,
                transaction_hash TEXT,
                occurred_at_ms INTEGER NOT NULL,
                previous_event_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS execution_audit_no_update
            BEFORE UPDATE ON execution_audit_events
            BEGIN SELECT RAISE(ABORT, 'execution audit is append-only'); END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS execution_audit_no_delete
            BEFORE DELETE ON execution_audit_events
            BEGIN SELECT RAISE(ABORT, 'execution audit is append-only'); END
        """)

    @staticmethod
    def append_on_connection(
        db: sqlite3.Connection,
        *,
        intent_id: str,
        event_type: str,
        state: ExecutionState | str,
        occurred_at_ms: int,
        reason_code: str | None = None,
        policy_hash: str | None = None,
        evidence_hash: str | None = None,
        transaction_hash: str | None = None,
    ) -> str:
        safe_intent = _safe_id(intent_id)
        if not isinstance(event_type, str) or not _CODE_RE.fullmatch(event_type):
            raise ValueError("event_type must be a lowercase code")
        state_value = state.value if isinstance(state, ExecutionState) else state
        if state_value not in {item.value for item in ExecutionState}:
            raise ValueError("state is invalid")
        if state_value in {item.value for item in _DISABLED}:
            raise ExecutionDisabled("signed, submitted and confirmed audit states are disabled in this release")
        stamp = _stamp(occurred_at_ms)
        reason = _safe_code(reason_code, "reason_code")
        policy = _safe_hash(policy_hash, "policy_hash")
        evidence = _safe_hash(evidence_hash, "evidence_hash")
        tx_hash = _safe_hash(transaction_hash, "transaction_hash")
        previous = db.execute(
            "SELECT event_hash FROM execution_audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous["event_hash"] if previous else "0" * 64
        event_id = uuid.uuid4().hex
        event = {
            "event_id": event_id,
            "intent_id": safe_intent,
            "event_type": event_type,
            "state": state_value,
            "reason_code": reason,
            "policy_hash": policy,
            "evidence_hash": evidence,
            "transaction_hash": tx_hash,
            "occurred_at_ms": stamp,
            "previous_event_hash": previous_hash,
        }
        event_hash = hashlib.sha256(
            json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        db.execute(
            """INSERT INTO execution_audit_events
               (event_id,intent_id,event_type,state,reason_code,policy_hash,
                evidence_hash,transaction_hash,occurred_at_ms,previous_event_hash,event_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id, safe_intent, event_type, state_value, reason, policy,
             evidence, tx_hash, stamp, previous_hash, event_hash),
        )
        return event_id

    def append(self, **event) -> str:
        with closing(self._connect()) as db, db:
            self._ensure_schema(db)
            db.execute("BEGIN IMMEDIATE")
            try:
                event_id = self.append_on_connection(db, **event)
                db.execute("COMMIT")
                return event_id
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def events(self, intent_id: str | None = None) -> list[dict]:
        with closing(self._connect()) as db, db:
            self._ensure_schema(db)
            if intent_id is None:
                rows = db.execute(
                    "SELECT * FROM execution_audit_events ORDER BY id"
                ).fetchall()
            else:
                safe_intent = _safe_id(intent_id)
                rows = db.execute(
                    "SELECT * FROM execution_audit_events WHERE intent_id=? ORDER BY id",
                    (safe_intent,),
                ).fetchall()
        result = [dict(row) for row in rows]
        previous_hash = "0" * 64
        for event in result:
            if event["previous_event_hash"] != previous_hash:
                raise AuditIntegrityError("execution audit hash-chain predecessor mismatch")
            payload = {
                key: event[key]
                for key in (
                    "event_id", "intent_id", "event_type", "state", "reason_code",
                    "policy_hash", "evidence_hash", "transaction_hash",
                    "occurred_at_ms", "previous_event_hash",
                )
            }
            expected_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
            ).hexdigest()
            if not isinstance(event["event_hash"], str) or not hmac.compare_digest(expected_hash, event["event_hash"]):
                raise AuditIntegrityError("execution audit event hash mismatch")
            previous_hash = event["event_hash"]
        return result


class SubmissionCoordinator:
    """Idempotent state coordinator with no signing or submission capability."""

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        if not self.database_path or self.database_path == ":memory:":
            raise ValueError("submission coordinator requires a persistent database")
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS execution_intents (
                    intent_id TEXT PRIMARY KEY,
                    chain TEXT NOT NULL,
                    wallet_public_key TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    intent_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason_code TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
            """)
            ExecutionAuditStore._ensure_schema(db)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    def create(
        self,
        *,
        intent_id: str,
        chain: str,
        wallet_public_key: str,
        policy_hash: str,
        intent_hash: str,
        created_at_ms: int,
    ) -> dict:
        safe_intent = _safe_id(intent_id)
        if not isinstance(chain, str) or not chain or chain.strip() != chain:
            raise ValueError("chain is malformed")
        if not isinstance(wallet_public_key, str) or not wallet_public_key or wallet_public_key.strip() != wallet_public_key:
            raise ValueError("wallet_public_key is malformed")
        policy = _safe_hash(policy_hash, "policy_hash")
        intent_digest = _safe_hash(intent_hash, "intent_hash")
        stamp = _stamp(created_at_ms)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM execution_intents WHERE intent_id=?",
                    (safe_intent,),
                ).fetchone()
                if row is not None:
                    if (
                        row["chain"] != chain
                        or row["wallet_public_key"] != wallet_public_key
                        or row["policy_hash"] != policy
                        or row["intent_hash"] != intent_digest
                    ):
                        raise IdempotencyConflict("intent ID was reused with different scope")
                    db.execute("COMMIT")
                    return dict(row)
                db.execute(
                    """INSERT INTO execution_intents
                       (intent_id,chain,wallet_public_key,policy_hash,intent_hash,
                        state,reason_code,created_at_ms,updated_at_ms)
                       VALUES (?,?,?,?,?,'CREATED',NULL,?,?)""",
                    (safe_intent, chain, wallet_public_key, policy, intent_digest, stamp, stamp),
                )
                ExecutionAuditStore.append_on_connection(
                    db, intent_id=safe_intent, event_type="intent_created",
                    state=ExecutionState.CREATED, occurred_at_ms=stamp,
                    policy_hash=policy, evidence_hash=intent_digest,
                )
                row = db.execute(
                    "SELECT * FROM execution_intents WHERE intent_id=?",
                    (safe_intent,),
                ).fetchone()
                db.execute("COMMIT")
                return dict(row)
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def transition(
        self,
        intent_id: str,
        *,
        expected_state: ExecutionState | str,
        next_state: ExecutionState | str,
        occurred_at_ms: int,
        reason_code: str | None = None,
        evidence_hash: str | None = None,
        transaction_hash: str | None = None,
    ) -> dict:
        safe_intent = _safe_id(intent_id)
        expected = expected_state.value if isinstance(expected_state, ExecutionState) else expected_state
        target = next_state if isinstance(next_state, ExecutionState) else ExecutionState(next_state)
        if target in _DISABLED:
            raise ExecutionDisabled("signing, submission and confirmation are disabled in this release")
        try:
            current = ExecutionState(expected)
        except ValueError as exc:
            raise ValueError("expected_state is invalid") from exc
        if target not in _ALLOWED[current]:
            raise ValueError("execution state transition is not allowed")
        stamp = _stamp(occurred_at_ms)
        reason = _safe_code(reason_code, "reason_code")
        evidence = _safe_hash(evidence_hash, "evidence_hash")
        tx_hash = _safe_hash(transaction_hash, "transaction_hash")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM execution_intents WHERE intent_id=?",
                    (safe_intent,),
                ).fetchone()
                if row is None:
                    raise KeyError("execution intent does not exist")
                if row["state"] != current.value:
                    raise RuntimeError("execution intent state changed concurrently")
                if current in _TERMINAL:
                    raise RuntimeError("terminal execution intent cannot be reopened")
                cursor = db.execute(
                    """UPDATE execution_intents
                       SET state=?,reason_code=?,updated_at_ms=?
                       WHERE intent_id=? AND state=?""",
                    (target.value, reason, stamp, safe_intent, current.value),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("execution intent transition lost a race")
                ExecutionAuditStore.append_on_connection(
                    db, intent_id=safe_intent, event_type="state_transition",
                    state=target, occurred_at_ms=stamp, reason_code=reason,
                    policy_hash=row["policy_hash"], evidence_hash=evidence,
                    transaction_hash=tx_hash,
                )
                updated = db.execute(
                    "SELECT * FROM execution_intents WHERE intent_id=?",
                    (safe_intent,),
                ).fetchone()
                db.execute("COMMIT")
                return dict(updated)
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def fail(self, intent_id: str, *, occurred_at_ms: int, reason_code: str) -> dict:
        row = self.get(intent_id)
        if row is None:
            raise KeyError("execution intent does not exist")
        current = ExecutionState(row["state"])
        if current in _TERMINAL:
            return row
        return self.transition(
            intent_id,
            expected_state=current,
            next_state=ExecutionState.FAILED,
            occurred_at_ms=occurred_at_ms,
            reason_code=reason_code,
        )

    def get(self, intent_id: str) -> dict | None:
        safe_intent = _safe_id(intent_id)
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT * FROM execution_intents WHERE intent_id=?",
                (safe_intent,),
            ).fetchone()
        return dict(row) if row else None
