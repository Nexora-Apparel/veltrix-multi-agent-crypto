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


def test_global_lease_excludes_second_owner_and_fences_expired_owner(tmp_path):
    store = store_at(tmp_path / "orchestration.sqlite3")
    first = store.acquire_lease("worker-a", now_ms=10_000, ttl_ms=2_000)
    with pytest.raises(LeaseBusy):
        store.acquire_lease("worker-b", now_ms=11_000, ttl_ms=2_000)
    second = store.acquire_lease("worker-b", now_ms=12_000, ttl_ms=2_000)
    assert second == first + 1
    assert not store.renew_lease("worker-a", first, now_ms=12_001)
    assert not store.release_lease("worker-a", first)
    assert store.release_lease("worker-b", second)


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
    job = store.enqueue(idempotency_key="retry-1", job_type="scan",
                        payload={}, max_attempts=2, now_ms=10_000)
    first = store.start_attempt(job["run_id"], now_ms=10_100)
    assert first["attempt"] == 1
    assert store.finish_attempt(job["run_id"], 1, outcome="RETRYABLE",
                                now_ms=10_200, retry_delay_ms=5_000) == "RETRYABLE"
    assert store.start_attempt(job["run_id"], now_ms=14_999) is None
    second = store.start_attempt(job["run_id"], now_ms=15_200)
    assert second["attempt"] == 2
    assert store.finish_attempt(job["run_id"], 2, outcome="RETRYABLE",
                                now_ms=15_300, retry_delay_ms=5_000) == "FAILED"
    assert store.get_job(job["run_id"])["error_code"] == "ATTEMPTS_EXHAUSTED"


def test_interrupted_run_recovers_only_after_stale_threshold(tmp_path):
    store = store_at(tmp_path / "recovery.sqlite3")
    job = store.enqueue(idempotency_key="recover-1", job_type="scan",
                        payload={}, now_ms=10_000)
    store.start_attempt(job["run_id"], now_ms=11_000)
    assert store.recover_interrupted(now_ms=310_999, stale_after_ms=300_000) == 0
    assert store.recover_interrupted(now_ms=311_000, stale_after_ms=300_000) == 1
    recovered = store.get_job(job["run_id"])
    assert recovered["status"] == "RETRYABLE"
    assert recovered["error_code"] == "INTERRUPTED"
    assert store.start_attempt(job["run_id"], now_ms=311_001)["attempt"] == 2


def test_run_once_executes_one_attempt_and_is_idempotent(tmp_path):
    store = store_at(tmp_path / "run-once.sqlite3")
    calls = []

    def handler(context, payload):
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


def test_context_enforces_item_budget_and_deadline():
    now = [100.0]
    from veltrix.orchestration import RunContext

    context = RunContext("r", "k", 1, 105.0, 2, lambda: now[0])
    context.checkpoint(items=2)
    with pytest.raises(JobDeadlineExceeded, match="item budget"):
        context.checkpoint()
    now[0] = 106.0
    fresh = RunContext("r", "k", 1, 105.0, 2, lambda: now[0])
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
