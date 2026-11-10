import threading

import pytest

from veltrix.solana_meme.pipeline_runtime import SolanaPipelineRuntime


class FakePipeline:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or type("Result", (), {"job": {"status": "SUCCEEDED", "executed": True, "id": "job-1"}})()
        self.error = error

    def run_once(self, *, idempotency_key):
        self.calls.append(idempotency_key)
        if self.error:
            raise self.error
        return self.result


def test_pipeline_runtime_persists_health_and_uses_stable_idempotency(tmp_path):
    import sqlite3
    path = tmp_path / "runtime.sqlite3"
    clock = iter([120.0, 120.0, 121.0, 121.0]).__next__
    pipeline = FakePipeline()
    runtime = SolanaPipelineRuntime(pipeline, str(path), poll_interval_seconds=60, clock=clock)
    result = runtime.run_once()
    assert result.job["status"] == "SUCCEEDED"
    assert pipeline.calls == ["solana-pipeline-19700101T000200Z"]
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT status,last_success_at_ms,consecutive_failures FROM solana_pipeline_health WHERE id=1").fetchone()
    assert row == ("healthy", 121000, 0)


def test_pipeline_runtime_records_failure_and_rethrows(tmp_path):
    import sqlite3
    path = tmp_path / "failure.sqlite3"
    clock = iter([180.0, 180.0, 181.0, 181.0]).__next__
    runtime = SolanaPipelineRuntime(FakePipeline(error=RuntimeError("provider timeout")),
                                    str(path), poll_interval_seconds=60, clock=clock)
    with pytest.raises(RuntimeError, match="provider timeout"):
        runtime.run_once()
    with sqlite3.connect(path) as db:
        row = db.execute("SELECT status,last_error,consecutive_failures FROM solana_pipeline_health WHERE id=1").fetchone()
    assert row == ("degraded", "RuntimeError: provider timeout", 1)


def test_pipeline_runtime_stops_without_running_when_stop_is_set(tmp_path):
    runtime = SolanaPipelineRuntime(FakePipeline(), str(tmp_path / "stopped.sqlite3"))
    stop = threading.Event()
    stop.set()
    runtime.run(stop)
