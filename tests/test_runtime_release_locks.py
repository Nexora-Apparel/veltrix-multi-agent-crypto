import pytest

from veltrix.solana_meme.consensus_runtime import build_runtime as build_consensus_runtime
from veltrix.solana_meme.intelligence_worker import build_runtime as build_intelligence_runtime
from veltrix.solana_meme.worker import build_engine as build_discovery_engine
from veltrix.solana_meme.risk_runtime import build_runtime as build_risk_runtime


@pytest.mark.parametrize(
    "name,value",
    [
        ("SCHEDULER_ENABLED", "true"),
        ("TELEGRAM_ENABLED", "true"),
        ("VERCEL_ENV", "production"),
    ],
)
@pytest.mark.parametrize("builder", ["discovery", "intelligence", "consensus"])
def test_long_running_runtime_builders_enforce_release_locks(
    tmp_path, monkeypatch, name, value, builder
):
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("VELTRIX_DEPLOYMENT_TARGET", "development")
    if builder == "discovery":
        call = lambda: build_discovery_engine(database_path=tmp_path / "discovery.sqlite3")
    elif builder == "intelligence":
        call = lambda: build_intelligence_runtime(database_path=tmp_path / "market.sqlite3")
    else:
        call = lambda: build_consensus_runtime(database_path=tmp_path / "consensus.sqlite3")

    with pytest.raises(RuntimeError):
        call()



def test_risk_runtime_enforces_release_locks_even_with_injected_settings(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("VELTRIX_DEPLOYMENT_TARGET", "development")
    with pytest.raises(RuntimeError):
        build_risk_runtime(
            settings_obj=object(),
            database_path=tmp_path / "risk.sqlite3",
        )
