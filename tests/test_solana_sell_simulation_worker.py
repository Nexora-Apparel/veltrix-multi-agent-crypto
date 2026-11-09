import pytest

from veltrix.providers.solana_sell_simulation import SolanaSellSimulationObservation
from veltrix.solana_meme.sell_simulation_store import SolanaSellSimulationStore
from veltrix.solana_meme.simulation_preflight_store import SolanaSimulationPreflightStore
from veltrix.providers.solana_simulation_preflight import SolanaSimulationWalletPreflight
from veltrix.solana_meme.sell_simulation_worker import SolanaSellSimulationWorker

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TAKER = "11111111111111111111111111111111"


class FakeProvider:
    taker = TAKER

    def __init__(self, state="SIMULATION_SUCCEEDED", fail=False):
        self.state = state
        self.fail = fail
        self.calls = []

    def simulate_sell(self, mint, amount):
        self.calls.append((mint, amount))
        if self.fail:
            raise RuntimeError("rpc unavailable")
        return SolanaSellSimulationObservation(
            mint=mint, taker=TAKER, request_id=f"req-{len(self.calls)}",
            observed_at_ms=10_000 + len(self.calls), simulated_slot=123,
            units_consumed=10_000, logs=("simulated",), state=self.state,
            error=None if self.state == "SIMULATION_SUCCEEDED" else "InstructionError",
        )


def test_worker_processes_only_explicit_cases_and_persists(tmp_path):
    db = str(tmp_path / "sim.sqlite")
    store = SolanaSellSimulationStore(db)
    provider = FakeProvider()
    worker = SolanaSellSimulationWorker(
        db, provider, store, {MINT: 500_000},
        clock_ms=lambda: 10_000, refresh_interval_ms=100,
    )
    result = worker.run_once()
    assert result.processed == 1
    assert result.succeeded == 1
    assert result.provider_failed == 0
    assert provider.calls == [(MINT, 500_000)]
    assert store.count(MINT) == 1
    assert store.latest(MINT)["report"]["transaction_submitted"] is False

    second = worker.run_once()
    assert second.processed == 0
    assert len(provider.calls) == 1


def test_worker_records_simulation_failure_as_observation_not_provider_failure(tmp_path):
    db = str(tmp_path / "sim.sqlite")
    store = SolanaSellSimulationStore(db)
    worker = SolanaSellSimulationWorker(
        db, FakeProvider(state="SIMULATION_FAILED"), store, {MINT: 100},
        clock_ms=lambda: 10_000,
    )
    result = worker.run_once()
    assert result.simulation_failed == 1
    assert result.provider_failed == 0
    assert store.latest(MINT)["state"] == "SIMULATION_FAILED"


def test_worker_retries_provider_failure_with_backoff(tmp_path):
    db = str(tmp_path / "sim.sqlite")
    store = SolanaSellSimulationStore(db)
    provider = FakeProvider(fail=True)
    worker = SolanaSellSimulationWorker(
        db, provider, store, {MINT: 100},
        clock_ms=lambda: 10_000, retry_base_ms=50, retry_max_ms=100,
    )
    result = worker.run_once()
    assert result.provider_failed == 1
    assert store.count() == 0
    assert worker.run_once().processed == 0


def test_rejects_unbounded_or_invalid_test_cases(tmp_path):
    db = str(tmp_path / "sim.sqlite")
    store = SolanaSellSimulationStore(db)
    with pytest.raises(ValueError):
        SolanaSellSimulationWorker(db, FakeProvider(), store, {})
    with pytest.raises(ValueError):
        SolanaSellSimulationWorker(db, FakeProvider(), store, {MINT: 0})



class FakePreflightProvider:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def fetch(self, mint, amount):
        self.calls.append((mint, amount))
        return SolanaSimulationWalletPreflight(
            mint=mint, wallet=TAKER, required_amount_raw=amount,
            token_balance_raw=0 if self.state != "BALANCE_READY" else amount,
            token_account_count=0 if self.state != "BALANCE_READY" else 1,
            sol_balance_lamports=100_000_000, finalized_slot=123,
            observed_at_ms=10_000, state=self.state,
            token_program_ids=(),
        )


def test_worker_blocks_jupiter_order_when_finalized_token_balance_is_insufficient(tmp_path):
    db = str(tmp_path / "sim.sqlite")
    evidence = SolanaSellSimulationStore(db)
    preflight_store = SolanaSimulationPreflightStore(db)
    provider = FakeProvider()
    preflight = FakePreflightProvider("INSUFFICIENT_TOKEN_BALANCE")
    worker = SolanaSellSimulationWorker(
        db, provider, evidence, {MINT: 500},
        preflight_provider=preflight, preflight_store=preflight_store,
        clock_ms=lambda: 10_000,
    )
    result = worker.run_once()
    assert result.preflight_blocked == 1
    assert result.processed == 1
    assert provider.calls == []
    assert preflight_store.count(MINT) == 1


def test_worker_runs_simulation_only_after_finalized_balance_preflight_passes(tmp_path):
    db = str(tmp_path / "sim.sqlite")
    evidence = SolanaSellSimulationStore(db)
    preflight_store = SolanaSimulationPreflightStore(db)
    provider = FakeProvider()
    preflight = FakePreflightProvider("BALANCE_READY")
    worker = SolanaSellSimulationWorker(
        db, provider, evidence, {MINT: 500},
        preflight_provider=preflight, preflight_store=preflight_store,
        clock_ms=lambda: 10_000,
    )
    result = worker.run_once()
    assert result.preflight_blocked == 0
    assert result.succeeded == 1
    assert provider.calls == [(MINT, 500)]
    assert preflight_store.count(MINT) == 1
