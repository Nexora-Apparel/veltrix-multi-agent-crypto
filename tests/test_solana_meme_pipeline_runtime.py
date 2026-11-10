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


def test_pipeline_runtime_enables_native_sol_only_when_explicit(monkeypatch, tmp_path):
    import veltrix.solana_meme.pipeline_runtime as runtime_module

    calls = []
    class Pipeline:
        def run_once(self, *, idempotency_key):
            return type("Result", (), {"job": {"status": "SUCCEEDED", "id": "native-sol-job"}})()

    monkeypatch.setattr(runtime_module, "shared_database_path", lambda path=None: tmp_path / "native.sqlite3")
    monkeypatch.setattr(
        runtime_module, "build_one_shot_pipeline",
        lambda **kwargs: calls.append(kwargs) or Pipeline(),
    )
    monkeypatch.setenv("SOL_PAPER_TRADING_ENABLED", "true")
    monkeypatch.setenv("PAPER_TRADING_ENABLED", "false")
    monkeypatch.setenv("LIVE_TRADING", "false")
    runtime = runtime_module.build_runtime()
    assert runtime.pipeline is not None
    assert calls[-1]["sol_paper_enabled"] is True
    assert calls[-1]["paper_enabled"] is False


def test_pipeline_runtime_rejects_mixed_or_live_execution_modes(monkeypatch, tmp_path):
    import veltrix.solana_meme.pipeline_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "shared_database_path", lambda path=None: tmp_path / "mode.sqlite3")
    monkeypatch.setenv("PAPER_TRADING_ENABLED", "true")
    monkeypatch.setenv("SOL_PAPER_TRADING_ENABLED", "true")
    monkeypatch.setenv("LIVE_TRADING", "false")
    with pytest.raises(RuntimeError, match="only one paper ledger"):
        runtime_module.build_runtime()

    monkeypatch.setenv("PAPER_TRADING_ENABLED", "false")
    monkeypatch.setenv("SOL_PAPER_TRADING_ENABLED", "false")
    monkeypatch.setenv("LIVE_TRADING", "true")
    with pytest.raises(RuntimeError, match="live execution is disabled"):
        runtime_module.build_runtime()
