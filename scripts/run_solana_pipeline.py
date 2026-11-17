"""Run one bounded, read-only Solana intelligence pipeline attempt."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
import re
import uuid
from urllib.parse import urlsplit

from veltrix.config import settings
from veltrix.database import is_remote_database
from veltrix.foundation import validate_runtime_controls
from veltrix.solana_meme.pipeline import build_one_shot_pipeline

CONFORMANCE_HOST = "veltrix-h0-conformance-test-nexora-apparel.aws-eu-central-1.turso.io"


def build_idempotency_key(environ: dict[str, str] | None = None) -> str:
    """Give each GitHub Actions dispatch/attempt a distinct durable job identity.

    Date/hour keys permanently deduplicate later dispatches against an earlier
    FAILED job. GitHub run IDs identify a dispatch; run attempts distinguish
    explicit Actions re-runs. Local invocations receive a fresh UUID.
    """
    env = os.environ if environ is None else environ
    run_id = str(env.get("GITHUB_RUN_ID", "")).strip()
    run_attempt = str(env.get("GITHUB_RUN_ATTEMPT", "1")).strip() or "1"
    if run_id:
        if not re.fullmatch(r"[0-9]+", run_id):
            raise ValueError("GITHUB_RUN_ID must contain only decimal digits")
        if not re.fullmatch(r"[0-9]+", run_attempt):
            raise ValueError("GITHUB_RUN_ATTEMPT must contain only decimal digits")
        return f"solana-intelligence-v2-{run_id}-attempt-{run_attempt}"
    return f"solana-intelligence-v2-manual-{uuid.uuid4()}"


def main() -> None:
    validate_runtime_controls()
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        raise SystemExit("production TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are required")
    if not is_remote_database(url):
        raise SystemExit("scheduled worker requires an explicit remote Turso database")
    parsed = urlsplit(url)
    if parsed.scheme != "libsql" or not (parsed.hostname or "").endswith(".turso.io"):
        raise SystemExit("TURSO_DATABASE_URL must be a libsql://*.turso.io URL")
    if parsed.hostname == CONFORMANCE_HOST:
        raise SystemExit("refusing to run the production pipeline against the disposable conformance database")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise SystemExit("TURSO_DATABASE_URL must not contain credentials or URL extras")
    if os.getenv("LIVE_TRADING", "false").lower() not in {"false", "0", "no", "off", ""}:
        raise SystemExit("LIVE_TRADING must remain false")
    if os.getenv("PAPER_TRADING_ENABLED", "false").lower() not in {"false", "0", "no", "off", ""}:
        raise SystemExit("PAPER_TRADING_ENABLED must remain false for this worker")
    if not os.getenv("SOLANA_RPC_URL", "").strip() and not os.getenv("HELIUS_API_KEY", "").strip():
        raise SystemExit("configure SOLANA_RPC_URL or HELIUS_API_KEY for Phase 05 risk evidence")

    pipeline = build_one_shot_pipeline(
        database_path=url,
        settings_obj=settings(),
        paper_enabled=False,
    )
    result = pipeline.run_once(idempotency_key=build_idempotency_key())
    payload = {
        "job": result.job,
        "stages": asdict(result.stages) if result.stages is not None else None,
        "stage_progress": list(result.stage_progress),
        "market_data_diagnostics": getattr(
            pipeline.enrichment.market_data, "last_diagnostics", {}
        ),
        "database_host": parsed.hostname,
        "paper_enabled": False,
        "live_execution_enabled": False,
    }
    print(json.dumps(payload, sort_keys=True, default=str))
    if result.job.get("status") not in {"SUCCEEDED"}:
        raise SystemExit("one-shot pipeline did not complete successfully")


if __name__ == "__main__":
    main()
