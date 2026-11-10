"""Canonical database identity shared by the Solana pipeline workers."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from ..database import is_remote_database

load_dotenv()

DEFAULT_SOLANA_DATABASE = "data/solana_discovery.sqlite3"


def shared_database_path(database_path: str | Path | None = None) -> str:
    """Resolve one persistent local path or one explicit remote DB URL."""
    if database_path is not None:
        selected = str(database_path).strip()
    else:
        discovery_path = os.getenv("VELTRIX_SOLANA_DISCOVERY_DB", "").strip()
        legacy_path = os.getenv("DB_PATH", "").strip()
        if discovery_path and legacy_path:
            if is_remote_database(discovery_path) or is_remote_database(legacy_path):
                same_database = discovery_path == legacy_path
            else:
                same_database = (
                    Path(discovery_path).expanduser().resolve()
                    == Path(legacy_path).expanduser().resolve()
                )
            if not same_database:
                raise ValueError(
                    "VELTRIX_SOLANA_DISCOVERY_DB and DB_PATH must reference the same SQLite file"
                )
        selected = discovery_path or legacy_path or DEFAULT_SOLANA_DATABASE

    if not selected or selected == ":memory:":
        raise ValueError("Solana pipeline requires one persistent database")
    if selected.startswith(("http://", "turso://")):
        raise ValueError("insecure or unsupported remote database URL scheme")
    return selected
