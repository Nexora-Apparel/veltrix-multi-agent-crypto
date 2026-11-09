from types import SimpleNamespace

import pytest

from veltrix.providers.factory import build_solana_risk_evidence_collector
from veltrix.providers.goplus import GoPlusTokenSafetyProvider
from veltrix.providers.holder_inventory import SolanaHolderInventoryProvider
from veltrix.providers.solana_mint import SolanaMintAuthorityProvider
from veltrix.solana_meme.risk_runtime import build_runtime


class FakeCollector:
    def collect(self, mint, now_ms=None):
        raise AssertionError("runtime build test must not collect")


def test_risk_provider_factory_requires_rpc_but_goplus_is_optional():
    no_rpc = SimpleNamespace(
        solana_rpc_url="", helius_api_key="", goplus_access_token="",
        safety_evidence_max_age_ms=300_000, holder_inventory_max_accounts=5000,
    )
    assert build_solana_risk_evidence_collector(no_rpc) is None

    with_rpc = SimpleNamespace(
        solana_rpc_url="https://rpc.example", helius_api_key="",
        goplus_access_token="", safety_evidence_max_age_ms=300_000,
        holder_inventory_max_accounts=5000,
    )
    collector = build_solana_risk_evidence_collector(with_rpc)
    assert collector is not None
    assert collector.goplus_provider is None
    assert isinstance(collector.holder_provider, SolanaHolderInventoryProvider)
    assert isinstance(collector.mint_provider, SolanaMintAuthorityProvider)


def test_risk_provider_factory_uses_helius_rpc_and_optional_goplus_token():
    config = SimpleNamespace(
        solana_rpc_url="", helius_api_key="helius-secret",
        goplus_access_token="goplus-secret",
        safety_evidence_max_age_ms=300_000, holder_inventory_max_accounts=5000,
    )
    collector = build_solana_risk_evidence_collector(config)
    assert collector.holder_provider.rpc_url == (
        "https://mainnet.helius-rpc.com/?api-key=helius-secret"
    )
    assert isinstance(collector.goplus_provider, GoPlusTokenSafetyProvider)
    assert collector.goplus_provider.access_token == "goplus-secret"


def test_risk_runtime_builds_shared_database_workers(tmp_path):
    config = SimpleNamespace(
        solana_rpc_url="", helius_api_key="", goplus_access_token="",
    )
    runtime = build_runtime(
        database_path=tmp_path / "runtime.sqlite3",
        settings_obj=config,
        collector=FakeCollector(),
        poll_interval_seconds=10,
        refresh_interval_ms=120_000,
        batch_size=3,
    )
    assert runtime.batch_size == 3
    assert runtime.poll_interval_seconds == 10
    assert runtime.worker.refresh_interval_ms == 120_000
    assert runtime.worker.database_path == str(tmp_path / "runtime.sqlite3")


def test_risk_runtime_requires_rpc_configuration_without_injected_collector(tmp_path):
    config = SimpleNamespace(solana_rpc_url="", helius_api_key="", goplus_access_token="")
    with pytest.raises(ValueError, match="Configure SOLANA_RPC_URL"):
        build_runtime(
            database_path=tmp_path / "runtime.sqlite3",
            settings_obj=config,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"poll_interval_seconds": 4},
        {"poll_interval_seconds": 3601},
        {"batch_size": 0},
        {"batch_size": 26},
        {"refresh_interval_ms": 0},
        {"refresh_interval_ms": 1.5},
        {"batch_size": 1.5},
    ],
)
def test_risk_runtime_rejects_invalid_configuration(tmp_path, kwargs):
    config = SimpleNamespace(
        solana_rpc_url="", helius_api_key="", goplus_access_token="",
    )
    with pytest.raises(ValueError):
        build_runtime(
            database_path=tmp_path / "invalid.sqlite3",
            settings_obj=config,
            collector=FakeCollector(),
            **kwargs,
        )
