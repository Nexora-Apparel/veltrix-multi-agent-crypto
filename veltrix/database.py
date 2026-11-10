"""Backend-neutral database connection boundary.

SQLite remains the default. Turso is selected only for an explicit libsql URL
and an explicit TURSO_AUTH_TOKEN; remote failures never fall back to a local DB.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any


def is_remote_database(database: str | Path) -> bool:
    """Return True only for secure remote database URL schemes."""
    value = str(database).strip().lower()
    return value.startswith(("libsql://", "https://", "http://", "turso://"))


def ensure_database_parent(database: str | Path) -> None:
    """Create a local database parent directory; remote URLs are never paths."""
    value = str(database).strip()
    if is_remote_database(value) or value == ":memory:" or value.startswith("file:"):
        return
    path = Path(value).expanduser()
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)


def _remote_driver():
    try:
        import turso_serverless
    except ImportError as exc:
        raise RuntimeError(
            "Turso was selected but turso-serverless is not installed"
        ) from exc
    return turso_serverless


def row_factory_for(database: str | Path):
    """Return the row factory matching the selected DB-API implementation."""
    if is_remote_database(database):
        return _remote_driver().Row
    return sqlite3.Row


def connect_database(
    database: str | Path,
    *,
    timeout: float = 10.0,
    uri: bool = False,
    isolation_level: str | None = "DEFERRED",
    auth_token: str | None = None,
):
    """Open local SQLite or explicit remote Turso without implicit fallback."""
    value = str(database).strip()
    if not value:
        raise ValueError("database path or URL is required")

    if is_remote_database(value):
        if uri:
            raise ValueError("SQLite URI mode is not supported for remote databases")
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"libsql", "https", "http", "turso"}
            or not host.endswith(".turso.io")
            or parsed.username
            or parsed.password
        ):
            raise ValueError("remote database URL host is not an approved Turso endpoint")
        # Normalize dashboard/client URL variants to secure schemes.
        # Plain HTTP is never sent over the network.
        if parsed.scheme == "http":
            value = parsed._replace(scheme="https").geturl()
        elif parsed.scheme == "turso":
            value = parsed._replace(scheme="libsql").geturl()
        token = (auth_token if auth_token is not None else os.getenv("TURSO_AUTH_TOKEN", "")).strip()
        if not token:
            raise RuntimeError(
                "Turso URL configured but TURSO_AUTH_TOKEN is missing"
            )
        driver = _remote_driver()
        # The HTTP driver has no local socket timeout parameter. The bounded
        # orchestration layer owns request/job deadlines.
        return driver.connect(
            value,
            auth_token=token,
            isolation_level=isolation_level,
        )

    if value.startswith(("http://", "turso://")):
        raise ValueError("insecure or unsupported remote database URL scheme")
    return sqlite3.connect(
        value,
        timeout=timeout,
        uri=uri,
        isolation_level=isolation_level,
    )


def database_backend(database: str | Path) -> str:
    """Stable, non-secret backend label for logs and health checks."""
    return "turso" if is_remote_database(database) else "sqlite"
