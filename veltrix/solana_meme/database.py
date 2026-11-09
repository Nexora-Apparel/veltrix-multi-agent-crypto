"""Canonical persistent SQLite path shared by the Solana pipeline workers."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load local runtime configuration before any phase resolves its shared database.
load_dotenv()


DEFAULT_SOLANA_DATABASE = "data/solana_discovery.sqlite3"


def shared_database_path(database_path: str | Path | None = None) -> str:
    """Resolve one DB path and reject conflicting legacy/new environment settings."""
    if database_path is not None:
        selected = str(database_path).strip()
    else:
        discovery_path = os.getenv("VELTRIX_SOLANA_DISCOVERY_DB", "").strip()
        legacy_path = os.getenv("DB_PATH", "").strip()
        if discovery_path and legacy_path:
            discovery_resolved = Path(discovery_path).expanduser().resolve()
            legacy_resolved = Path(legacy_path).expanduser().resolve()
            if discovery_resolved != legacy_resolved:
                raise ValueError(
                    "VELTRIX_SOLANA_DISCOVERY_DB and DB_PATH must reference the same SQLite file"
                )
        selected = discovery_path or legacy_path or DEFAULT_SOLANA_DATABASE

    if not selected or selected == ":memory:":
        raise ValueError("Solana pipeline requires one persistent SQLite database path")
    return selected
