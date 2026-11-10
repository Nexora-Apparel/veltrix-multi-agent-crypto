"""Live H0 compatibility checks for the remote Turso DB-API driver.

This test writes only to a dedicated, namespaced conformance table. Use a
disposable Turso database, never the production paper ledger.
"""
from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit

import pytest


DATABASE_URL = os.getenv("TURSO_CONFORMANCE_DATABASE_URL", "").strip()
AUTH_TOKEN = os.getenv("TURSO_CONFORMANCE_AUTH_TOKEN", "").strip()
EXPECTED_HOST = "veltrix-h0-conformance.turso.io"


def _assert_disposable_target(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "libsql"
        or parsed.hostname != EXPECTED_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise RuntimeError(f"remote conformance is restricted to {EXPECTED_HOST}")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not AUTH_TOKEN,
    reason="dedicated disposable Turso conformance credentials are required",
)


def test_remote_driver_supports_veltrix_sqlite_contract() -> None:
    _assert_disposable_target(DATABASE_URL)
    driver = pytest.importorskip("turso_serverless")
    assert driver.apilevel == "2.0"
    assert driver.paramstyle == "qmark"

    run_id = uuid.uuid4().hex
    rollback_id = f"{run_id}-rollback"
    committed_id = f"{run_id}-committed"
    batch_ids = [f"{run_id}-batch-1", f"{run_id}-batch-2"]

    connection = driver.connect(DATABASE_URL, auth_token=AUTH_TOKEN)
    try:
        # The application uses sqlite3.Row-style named-column access.
        connection.row_factory = driver.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS veltrix_h0_driver_conformance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_veltrix_h0_driver_run
            ON veltrix_h0_driver_conformance(run_id)
            """
        )
        connection.commit()

        # The application uses BEGIN IMMEDIATE for durable worker claims.
        connection.execute("BEGIN IMMEDIATE")
        rollback_cursor = connection.execute(
            """
            INSERT INTO veltrix_h0_driver_conformance
                (run_id, payload, created_at_ms)
            VALUES (?, ?, ?)
            """,
            (rollback_id, "must-rollback", 1),
        )
        assert rollback_cursor.rowcount == 1
        assert rollback_cursor.lastrowid is not None
        connection.rollback()

        rolled_back = connection.execute(
            "SELECT run_id FROM veltrix_h0_driver_conformance WHERE run_id = ?",
            (rollback_id,),
        ).fetchone()
        assert rolled_back is None

        # Multi-statement transaction and named-row access.
        connection.execute("BEGIN IMMEDIATE")
        insert_cursor = connection.execute(
            """
            INSERT INTO veltrix_h0_driver_conformance
                (run_id, payload, created_at_ms)
            VALUES (?, ?, ?)
            """,
            (committed_id, "committed", 2),
        )
        assert insert_cursor.rowcount == 1
        assert insert_cursor.lastrowid is not None
        connection.commit()

        row = connection.execute(
            """
            SELECT id, run_id, payload, created_at_ms
            FROM veltrix_h0_driver_conformance
            WHERE run_id = ?
            """,
            (committed_id,),
        ).fetchone()
        assert row is not None
        assert row["run_id"] == committed_id
        assert row["payload"] == "committed"
        assert row["created_at_ms"] == 2
        assert "payload" in row.keys()

        # executemany and affected-row counts are used by bulk persistence paths.
        batch_cursor = connection.executemany(
            """
            INSERT INTO veltrix_h0_driver_conformance
                (run_id, payload, created_at_ms)
            VALUES (?, ?, ?)
            """,
            [(batch_ids[0], "batch-1", 3), (batch_ids[1], "batch-2", 4)],
        )
        assert batch_cursor.rowcount == 2
        connection.commit()

        # Unique constraints must be surfaced as DB-API IntegrityError.
        with pytest.raises(driver.IntegrityError):
            connection.execute(
                """
                INSERT INTO veltrix_h0_driver_conformance
                    (run_id, payload, created_at_ms)
                VALUES (?, ?, ?)
                """,
                (committed_id, "duplicate", 5),
            )
        connection.rollback()

        count = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM veltrix_h0_driver_conformance
            WHERE run_id IN (?, ?, ?)
            """,
            (committed_id, *batch_ids),
        ).fetchone()
        assert count is not None
        assert count["total"] == 3
    finally:
        try:
            connection.rollback()
            connection.execute(
                """
                DELETE FROM veltrix_h0_driver_conformance
                WHERE run_id IN (?, ?, ?, ?)
                """,
                (rollback_id, committed_id, *batch_ids),
            )
            connection.commit()
        finally:
            connection.close()
