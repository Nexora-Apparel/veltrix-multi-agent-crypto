"""H2 live conformance against the dedicated disposable Frankfurt Turso DB only."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

URL = os.getenv("TURSO_CONFORMANCE_DATABASE_URL", "").strip()
TOKEN = os.getenv("TURSO_CONFORMANCE_AUTH_TOKEN", "").strip()
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
        raise RuntimeError(
            "H2 conformance is restricted to the dedicated disposable "
            f"database host {EXPECTED_HOST}"
        )


pytestmark = pytest.mark.skipif(
    not URL or not TOKEN,
    reason="dedicated disposable Turso conformance credentials are required",
)


def test_orchestration_schema_atomic_lease_idempotency_and_recovery(monkeypatch):
    _assert_disposable_target(URL)
    monkeypatch.setenv("TURSO_AUTH_TOKEN", TOKEN)
    from veltrix.orchestration import LeaseBusy, OrchestrationStore

    store = OrchestrationStore(URL)
    store.initialize()
    try:
        fence = store.acquire_lease("h2-worker-a", now_ms=1_000_000, ttl_ms=10_000)
        with pytest.raises(LeaseBusy):
            store.acquire_lease("h2-worker-b", now_ms=1_001_000, ttl_ms=10_000)
        assert store.renew_lease("h2-worker-a", fence, now_ms=1_002_000, ttl_ms=10_000)

        job = store.enqueue(
            idempotency_key="h2-conformance-once",
            job_type="conformance",
            payload={"safe": True},
            max_attempts=2,
            now_ms=1_000_000,
        )
        duplicate = store.enqueue(
            idempotency_key="h2-conformance-once",
            job_type="conformance",
            payload={"safe": True},
            max_attempts=2,
            now_ms=1_000_001,
        )
        assert duplicate["run_id"] == job["run_id"]
        attempt = store.start_attempt(job["run_id"], owner_id="h2-worker-a", fencing_token=fence, now_ms=1_000_002)
        assert attempt["attempt"] == 1
        assert store.finish_attempt(
            job["run_id"], 1, owner_id="h2-worker-a", fencing_token=fence, outcome="RETRYABLE",
            now_ms=1_000_003, retry_delay_ms=1_000,
        ) == "RETRYABLE"
        assert store.start_attempt(job["run_id"], owner_id="h2-worker-a", fencing_token=fence, now_ms=1_000_500) is None
        assert store.start_attempt(job["run_id"], owner_id="h2-worker-a", fencing_token=fence, now_ms=1_001_004)["attempt"] == 2
        assert store.finish_attempt(
            job["run_id"], 2, owner_id="h2-worker-a", fencing_token=fence, outcome="SUCCEEDED", now_ms=1_001_005,
        ) == "SUCCEEDED"
        assert store.get_job(job["run_id"])["status"] == "SUCCEEDED"
        assert store.release_lease("h2-worker-a", fence)
        next_fence = store.acquire_lease(
            "h2-worker-b", now_ms=1_002_001, ttl_ms=10_000
        )
        assert next_fence == fence + 1
        assert store.release_lease("h2-worker-b", next_fence)
    finally:
        import turso_serverless

        db = turso_serverless.connect(URL, auth_token=TOKEN)
        try:
            for table in (
                "orchestration_audit",
                "orchestration_worker_lease",
                "orchestration_jobs",
            ):
                db.execute(f'DROP TABLE IF EXISTS "{table}"')
            db.commit()
        finally:
            db.close()
