from __future__ import annotations

import threading

import pytest

from veltrix.orchestration import (
    JobDeadlineExceeded,
    LeaseBusy,
    OrchestrationStore,
    RetryableJobError,
    run_once,
)


def store_at(path):
    store = OrchestrationStore(str(path))
    store.initialize()
    return store


def leased(store, owner="worker-a", now=10_000, ttl=300_000):
    return store.acquire_lease(owner, now_ms=now, ttl_ms=ttl)


def test_global_lease_excludes_second_owner_and_fences_expired_owner(tmp_path):
    store = store_at(tmp_path / "orchestration.sqlite3")
    first = leased(store, "worker-a", now=10_000, ttl=2_000)
    with pytest.raises(LeaseBusy):
        store.acquire_lease("worker-b", now_ms=11_000, ttl_ms=2_000)
    second = store.acquire_lease("worker-b", now_ms=12_000, ttl_ms=2_000)
    assert second == first + 1
    assert not store.renew_lease("worker-a", first, now_ms=12_001)
    assert not store.release_lease("worker-a", first)
    assert store.release_lease("worker-b", second)
    third = store.acquire_lease("worker-c", now_ms=13_000, ttl_ms=2_000)
    assert third == second + 1
    assert store.release_lease("worker-c", third)


def test_concurrent_lease_acquisition_has_one_winner(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    store_at(path)
    barrier = threading.Barrier(3)
    outcomes = []
    lock = threading.Lock()

    def contender(name):
        store = OrchestrationStore(str(path))
        barrier.wait()
        try:
            result = ("won", store.acquire_lease(name, now_ms=50_000, ttl_ms=30_000))
        except LeaseBusy:
            result = ("busy", None)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=contender, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
    assert sum(result[0] == "won" for result in outcomes) == 1
    assert sum(result[0] == "busy" for result in outcomes) == 1


def test_stale_fencing_token_cannot_start_finish_or_recover(tmp_path):
    store = store_at(tmp_path / "fencing.sqlite3")
    first = leased(store, "worker-a", now=10_000, ttl=2_000)
    job = store.enqueue(idempotency_key="fence-job", job_type="scan", payload={}, now_ms=10_001)
    second = store.acquire_lease("worker-b", now_ms=12_000, ttl_ms=30_000)
    with pytest.raises(LeaseBusy):
        store.start_attempt(job["run_id"], owner_id="worker-a", fencing_token=first, now_ms=12_001)
    attempt = store.start_attempt(job["run_id"], owner_id="worker-b", fencing_token=second, now_ms=12_002)
    assert attempt["attempt"] == 1
    with pytest.raises(LeaseBusy):
        store.finish_attempt(job["run_id"], 1, owner_id="worker-a",
                             fencing_token=first, outcome="SUCCEEDED", now_ms=12_003)
    with pytest.raises(LeaseBusy):
        store.recover_interrupted(owner_id="worker-a", fencing_token=first,
                                  now_ms=400_000, stale_after_ms=1_000)
    assert store.finish_attempt(job["run_id"], 1, owner_id="worker-b",
                                fencing_token=second, outcome="SUCCEEDED", now_ms=12_004) == "SUCCEEDED"


def test_idempotency_key_rejects_changed_payload(tmp_path):
    store = store_at(tmp_path / "jobs.sqlite3")
    first = store.enqueue(idempotency_key="daily-scan-2026-11-10",
                          job_type="scan", payload={"limit": 25}, now_ms=10_000)
    duplicate = store.enqueue(idempotency_key="daily-scan-2026-11-10",
                              job_type="scan", payload={"limit": 25}, now_ms=11_000)
    assert duplicate["run_id"] == first["run_id"]
    with pytest.raises(ValueError, match="different job payload"):
        store.enqueue(idempotency_key="daily-scan-2026-11-10",
                      job_type="scan", payload={"limit": 50}, now_ms=12_000)


def test_retry_is_deferred_and_attempts_are_bounded(tmp_path):
    store = store_at(tmp_path / "retry.sqlite3")
    fence = leased(store)
    owner = "worker-a"
    job = store.enqueue(idempotency_key="retry-1", job_type="scan",
                        payload={}, max_attempts=2, now_ms=10_000)
    first = store.start_attempt(job["run_id"], owner_id=owner, fencing_token=fence, now_ms=10_100)
    assert first["attempt"] == 1
    assert store.finish_attempt(job["run_id"], 1, owner_id=owner, fencing_token=fence,
                                outcome="RETRYABLE", now_ms=10_200, retry_delay_ms=5_000) == "RETRYABLE"
    assert store.start_attempt(job["run_id"], owner_id=owner, fencing_token=fence, now_ms=14_999) is None
    second = store.start_attempt(job["run_id"], owner_id=owner, fencing_token=fence, now_ms=15_200)
    assert second["attempt"] == 2
    assert store.finish_attempt(job["run_id"], 2, owner_id=owner, fencing_token=fence,
                                outcome="RETRYABLE", now_ms=15_300, retry_delay_ms=5_000) == "FAILED"
    assert store.get_job(job["run_id"])["error_code"] == "ATTEMPTS_EXHAUSTED"


def test_interrupted_run_recovery_requires_current_lease(tmp_path):
    store = store_at(tmp_path / "recovery.sqlite3")
    job = store.enqueue(idempotency_key="recover-1", job_type="scan",
                        payload={}, now_ms=10_000)
    with pytest.raises(TypeError):
        store.recover_interrupted(now_ms=211_000, stale_after_ms=200_000)
    fence = leased(store, now=11_000)
    store.start_attempt(job["run_id"], owner_id="worker-a", fencing_token=fence, now_ms=11_000)
    assert store.recover_interrupted(owner_id="worker-a", fencing_token=fence,
                                     now_ms=210_999, stale_after_ms=200_000) == 0
    assert store.recover_interrupted(owner_id="worker-a", fencing_token=fence,
                                     now_ms=211_000, stale_after_ms=200_000) == 1
    recovered = store.get_job(job["run_id"])
    assert recovered["status"] == "RETRYABLE"
    assert recovered["error_code"] == "INTERRUPTED"
    assert store.start_attempt(job["run_id"], owner_id="worker-a",
                               fencing_token=fence, now_ms=211_001)["attempt"] == 2


def test_run_once_executes_one_attempt_and_is_idempotent(tmp_path):
    store = store_at(tmp_path / "run-once.sqlite3")
    calls = []

    def handler(context, payload):
        context.assert_fence()
        calls.append(payload["n"])
        context.checkpoint(items=1)

    first = run_once(store, idempotency_key="scan-unique", job_type="scan",
                     payload={"n": 7}, handler=handler)
    second = run_once(store, idempotency_key="scan-unique", job_type="scan",
                      payload={"n": 7}, handler=handler)
    assert first["status"] == "SUCCEEDED"
    assert second["executed"] is False
    assert calls == [7]


def test_retryable_handler_error_schedules_later_attempt(tmp_path):
    store = store_at(tmp_path / "run-retry.sqlite3")

    def handler(context, payload):
        raise RetryableJobError("provider unavailable")

    result = run_once(store, idempotency_key="provider-scan", job_type="scan",
                      payload={}, handler=handler, retry_delay_ms=5_000)
    assert result["status"] == "RETRYABLE"
    row = store.get_job(result["run_id"])
    assert row["attempt"] == 1
    assert row["error_code"] == "RetryableJobError"


def test_context_enforces_item_budget_deadline_and_fence():
    now = [100.0]
    from veltrix.orchestration import RunContext

    calls = []
    context = RunContext("r", "k", 1, 105.0, 2, lambda: now[0], 9,
                         lambda: calls.append("fenced"))
    context.checkpoint(items=2)
    assert calls == ["fenced"]
    with pytest.raises(JobDeadlineExceeded, match="item budget"):
        context.checkpoint()
    now[0] = 106.0
    fresh = RunContext("r", "k", 1, 105.0, 2, lambda: now[0], 9, lambda: None)
    with pytest.raises(JobDeadlineExceeded, match="deadline"):
        fresh.checkpoint(items=0)


def test_runtime_must_fit_inside_lease(tmp_path):
    store = store_at(tmp_path / "lease-margin.sqlite3")
    with pytest.raises(ValueError, match="lease_ttl_ms"):
        run_once(store, idempotency_key="x", job_type="scan", payload={},
                 handler=lambda context, payload: None,
                 max_runtime_seconds=60, lease_ttl_ms=60_000)


def test_payload_digest_requires_finite_json_data(tmp_path):
    store = store_at(tmp_path / "payload-validation.sqlite3")
    with pytest.raises(ValueError, match="JSON-serializable"):
        store.enqueue(idempotency_key="bad-payload", job_type="scan",
                      payload={"not_json": object()})
    with pytest.raises(ValueError, match="JSON-serializable"):
        store.enqueue(idempotency_key="nan-payload", job_type="scan",
                      payload={"not_finite": float("nan")})


def test_lease_renewal_rejects_unbounded_ttl(tmp_path):
    store = store_at(tmp_path / "lease-validation.sqlite3")
    fence = store.acquire_lease("worker", ttl_ms=10_000)
    with pytest.raises(ValueError, match="ttl_ms"):
        store.renew_lease("worker", fence, ttl_ms=900_000)
    assert store.release_lease("worker", fence)


def test_audit_log_is_append_only_and_error_codes_are_allowlisted(tmp_path):
    store = store_at(tmp_path / "audit.sqlite3")
    fence = leased(store)
    job = store.enqueue(idempotency_key="audit-key", job_type="scan",
                        payload={}, now_ms=10_001)
    attempt = store.start_attempt(job["run_id"], owner_id="worker-a",
                                  fencing_token=fence, now_ms=10_002)
    store.finish_attempt(job["run_id"], attempt["attempt"], owner_id="worker-a",
                         fencing_token=fence, outcome="FAILED",
                         error_code="SECRET_PROVIDER_RESPONSE",
                         now_ms=10_003)
    db = store._connect()
    try:
        row = db.execute(
            "SELECT error_code FROM orchestration_jobs WHERE run_id=?",
            (job["run_id"],),
        ).fetchone()
        assert row["error_code"] == "HANDLER_ERROR"
        with pytest.raises(Exception, match="append-only"):
            db.execute("UPDATE orchestration_audit SET detail_json='{}'")
        db.rollback()
        with pytest.raises(Exception, match="append-only"):
            db.execute("DELETE FROM orchestration_audit")
        db.rollback()
    finally:
        db.close()


def test_deadline_exhaustion_is_deferred_for_later_invocation(tmp_path):
    store = store_at(tmp_path / "deadline-retry.sqlite3")
    clock = [100.0]

    def handler(context, payload):
        clock[0] = 102.0
        context.checkpoint(items=1)

    result = run_once(
        store,
        idempotency_key="deadline-retry",
        job_type="scan",
        payload={},
        handler=handler,
        max_runtime_seconds=1,
        monotonic=lambda: clock[0],
        retry_delay_ms=5_000,
    )
    assert result["status"] == "RETRYABLE"
    job = store.get_job(result["run_id"])
    assert job["error_code"] == "JobDeadlineExceeded"
    assert job["next_attempt_at_ms"] is not None
