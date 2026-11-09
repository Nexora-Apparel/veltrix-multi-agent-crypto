import threading

import pytest

from veltrix.solana_meme.paper_runtime import PaperTradingRuntime, build_runtime


def test_paper_runtime_builds_all_stores_on_one_persistent_database(tmp_path, monkeypatch):
    monkeypatch.setenv("VELTRIX_PAPER_FEE_BPS", "125")
    monkeypatch.setenv("VELTRIX_PAPER_SLIPPAGE_BPS", "60")
    monkeypatch.setenv("VELTRIX_PAPER_MAX_DECISION_AGE_MS", "90000")
    monkeypatch.setenv("VELTRIX_PAPER_MAX_MARKET_AGE_MS", "60000")
    monkeypatch.setenv("VELTRIX_PAPER_MAX_RISK_AGE_MS", "180000")
    database = tmp_path / "runtime" / "paper.sqlite3"

    runtime = build_runtime(
        database_path=database,
        starting_balance=750,
        max_positions=4,
        max_position_usd=35,
        poll_interval_seconds=10,
        batch_size=15,
        clock_ms=lambda: 10_000,
    )

    worker = runtime.worker
    assert isinstance(runtime, PaperTradingRuntime)
    assert worker.broker.db_path == str(database)
    assert worker.market_store.database_path == str(database)
    assert worker.risk_store.database_path == str(database)
    assert worker.consensus_store.database_path == str(database)
    assert worker.broker.snapshot()["cash"] == 750
    assert worker.policy.position_usd == 35
    assert worker.policy.fee_bps == 125
    assert worker.policy.slippage_bps == 60
    assert worker.policy.max_decision_age_ms == 90_000
    assert worker.policy.max_market_age_ms == 60_000
    assert worker.policy.max_risk_age_ms == 180_000


def test_paper_runtime_runs_one_empty_cycle_without_live_execution(tmp_path):
    runtime = build_runtime(
        database_path=tmp_path / "empty.sqlite3",
        clock_ms=lambda: 10_000,
    )

    result = runtime.run_once()

    assert result.paper.scanned_decisions == 0
    assert result.paper.opened_positions == 0
    assert runtime.worker.broker.snapshot()["open_positions"] == []


def test_paper_runtime_stops_cleanly_when_stop_event_is_set(tmp_path):
    runtime = build_runtime(database_path=tmp_path / "stopped.sqlite3")
    stop = threading.Event()
    stop.set()

    runtime.run(stop)

    assert runtime.worker.broker.snapshot()["open_positions"] == []


@pytest.mark.parametrize("poll", [0, 4, 3601, True, float("nan"), float("inf")])
def test_paper_runtime_rejects_invalid_poll_intervals(tmp_path, poll):
    runtime = build_runtime(
        database_path=tmp_path / f"poll-{str(poll)}.sqlite3",
        poll_interval_seconds=15,
    )
    with pytest.raises(ValueError):
        PaperTradingRuntime(runtime.worker, poll_interval_seconds=poll)


@pytest.mark.parametrize("batch", [0, -1, 251, True, 1.5])
def test_paper_runtime_rejects_invalid_batch_sizes(tmp_path, batch):
    runtime = build_runtime(database_path=tmp_path / f"batch-{str(batch)}.sqlite3")
    with pytest.raises(ValueError):
        PaperTradingRuntime(runtime.worker, batch_size=batch)


def test_paper_runtime_rejects_non_numeric_environment_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("VELTRIX_PAPER_FEE_BPS", "not-a-number")
    with pytest.raises(ValueError, match="VELTRIX_PAPER_FEE_BPS"):
        build_runtime(database_path=tmp_path / "bad-fee.sqlite3")

def test_paper_runtime_survives_one_failed_cycle_and_stops_cleanly(caplog):
    class FailingOnceWorker:
        def __init__(self):
            self.calls = 0

        def run_once(self, *, limit):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary sqlite lock")
            raise AssertionError("stop event should end the loop after the first failure")

    class StopAfterWait:
        def is_set(self):
            return False

        def wait(self, seconds):
            assert seconds == 5
            return True

    runtime = PaperTradingRuntime(FailingOnceWorker(), poll_interval_seconds=5)
    runtime.run(StopAfterWait())
    assert "paper cycle failed safely" in caplog.text

def test_paper_runtime_refuses_to_start_when_live_trading_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING", "true")
    with pytest.raises(RuntimeError, match="LIVE_TRADING is enabled"):
        build_runtime(database_path=tmp_path / "live-guard.sqlite3")

def test_paper_runtime_reuses_existing_db_path_when_shared_path_is_unset(tmp_path, monkeypatch):
    database = tmp_path / "legacy-paper-ledger.sqlite3"
    monkeypatch.delenv("VELTRIX_SOLANA_DISCOVERY_DB", raising=False)
    monkeypatch.setenv("DB_PATH", str(database))

    runtime = build_runtime(clock_ms=lambda: 10_000)

    assert runtime.worker.broker.db_path == str(database)
    assert runtime.worker.market_store.database_path == str(database)
    assert runtime.worker.consensus_store.database_path == str(database)


def test_paper_runtime_rejects_split_legacy_and_discovery_databases(tmp_path, monkeypatch):
    monkeypatch.setenv("VELTRIX_SOLANA_DISCOVERY_DB", str(tmp_path / "discovery.sqlite3"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy-paper.sqlite3"))

    with pytest.raises(ValueError, match="must reference the same SQLite file"):
        build_runtime(clock_ms=lambda: 10_000)
