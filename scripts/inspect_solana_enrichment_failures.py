"""Read-only, redacted diagnostics for the Solana enrichment queue.

Uses the Turso read-only credential supplied by the workflow. This module
executes SELECT statements only and never changes queue state or candidate data.
"""
from __future__ import annotations

from collections import Counter
import json
import os
import re
from urllib.parse import urlsplit


def redact_error(value: object) -> str:
    """Return useful error context without addresses, URLs or credential values."""
    message = str(value or "unknown error")
    message = re.sub(r"(?i)bearer\\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", message)
    message = re.sub(
        r"(?i)(authorization|access[_-]?token|api[_-]?key|secret)\\s*[:=]\\s*[^\\s,;]+",
        r"\\1=[REDACTED]",
        message,
    )
    message = re.sub(r"https?://[^\\s)\\]]+", "[URL]", message)
    # Solana/base58-like identifiers should not be emitted in Actions logs.
    message = re.sub(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])", "[ADDRESS]", message)
    return message[:220]


def main() -> None:
    database_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("TURSO_DATABASE_URL is required")
    # connect_database reads TURSO_AUTH_TOKEN; the workflow maps its
    # read-only token to this variable for this diagnostic step only.
    from veltrix.database import connect_database, row_factory_for

    db = connect_database(database_url, isolation_level=None)
    db.row_factory = row_factory_for(database_url)
    try:
        statuses = db.execute(
            "SELECT status, COUNT(*) AS count FROM solana_enrichment_jobs GROUP BY status"
        ).fetchall()
        rows = db.execute(
            """SELECT last_error FROM solana_enrichment_jobs
               WHERE last_error IS NOT NULL
               ORDER BY updated_at_ms DESC LIMIT 250"""
        ).fetchall()
    finally:
        db.close()

    groups = Counter()
    for row in rows:
        raw = row["last_error"]
        error_type, separator, detail = str(raw).partition(":")
        groups[(error_type.strip() or "UnknownError", redact_error(detail if separator else raw))] += 1

    print(json.dumps({
        "diagnostic": "solana_enrichment_queue_read_only",
        "query_mode": "SELECT_ONLY",
        "status_counts": {row["status"]: int(row["count"]) for row in statuses},
        "recent_error_groups": [
            {"error_type": error_type, "message": message, "count": count}
            for (error_type, message), count in groups.most_common(20)
        ],
        "errors_sampled": len(rows),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
