from types import SimpleNamespace

import pytest

from veltrix.providers.alchemy import AlchemyWalletProvider
from veltrix.providers.factory import build_wallet_providers
from veltrix.providers.helius import HeliusWalletProvider


def settings(**overrides):
    values = {
        "helius_api_key": "", "tracked_smart_wallets": (),
        "alchemy_api_key": "", "tracked_evm_wallets": (),
        "alchemy_quote_tokens_base": (), "alchemy_quote_tokens_bsc": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_wallet_provider_factory_is_empty_without_credentials_and_wallets():
    assert build_wallet_providers(settings()) == ()


def test_wallet_provider_factory_builds_only_configured_connectors():
    solana = build_wallet_providers(settings(
        helius_api_key="helius-secret", tracked_smart_wallets=("wallet1",)
    ))
    assert len(solana) == 1
    assert isinstance(solana[0], HeliusWalletProvider)

    evm = build_wallet_providers(settings(
        alchemy_api_key="alchemy-secret", tracked_evm_wallets=("0xwallet",),
        alchemy_quote_tokens_base=("0xusdc",),
    ))
    assert len(evm) == 1
    assert isinstance(evm[0], AlchemyWalletProvider)


def test_wallet_provider_factory_builds_both_without_enabling_provider_trust():
    providers = build_wallet_providers(settings(
        helius_api_key="helius-secret", tracked_smart_wallets=("wallet1",),
        alchemy_api_key="alchemy-secret", tracked_evm_wallets=("0xwallet",),
    ))
    assert {provider.provider_id for provider in providers} == {"helius", "alchemy"}



def test_base_liquidity_factory_builder_injects_pinned_identity_without_completeness(monkeypatch):
    import json
    from veltrix.providers import evm_liquidity_reader
    from veltrix.providers.factory import build_liquidity_snapshot_providers
    from veltrix.base_factory_registry import BASE_UNISWAP_V2_FACTORY

    captured = {}

    class ReaderStub:
        def __init__(self, chain, pool_url, locker_url, factories, lockers, **kwargs):
            captured.update(chain=chain, factories=factories, kwargs=kwargs)

    monkeypatch.setattr(
        evm_liquidity_reader, "EvmV2LiquiditySnapshotReader", ReaderStub
    )
    configured = settings(
        base_liquidity_pool_rpc_url="https://pool.example",
        base_liquidity_locker_rpc_url="https://locker.example",
        base_liquidity_factories_json="",
        base_liquidity_lockers_json=json.dumps([{
            "amm_id": "uniswap-v2", "address": "0x" + "55" * 20,
            "deployment_block": 1, "expected_code_hash": "11" * 32,
            "abi_profile": "uncx-v2.1-locks-nonce",
        }]),
        base_liquidity_registry_complete=True,
    )
    build_liquidity_snapshot_providers(configured, chains=("base",))
    factory = captured["factories"][0]
    assert factory.amm_id == BASE_UNISWAP_V2_FACTORY.amm_id
    assert factory.chain_id == BASE_UNISWAP_V2_FACTORY.chain_id
    assert factory.creation_tx == BASE_UNISWAP_V2_FACTORY.creation_tx
    assert factory.source_ref == BASE_UNISWAP_V2_FACTORY.source_ref
    assert captured["kwargs"]["registry_complete"] is False


def test_base_liquidity_factory_builder_rejects_conflicting_pinned_identity(monkeypatch):
    import json
    from veltrix.providers import evm_liquidity_reader
    from veltrix.providers.factory import build_liquidity_snapshot_providers

    class ReaderStub:
        def __init__(self, *args, **kwargs):
            raise AssertionError("reader must not be constructed for conflicting identity")

    monkeypatch.setattr(
        evm_liquidity_reader, "EvmV2LiquiditySnapshotReader", ReaderStub
    )
    configured = settings(
        base_liquidity_pool_rpc_url="https://pool.example",
        base_liquidity_locker_rpc_url="https://locker.example",
        base_liquidity_factories_json=json.dumps([{
            "amm_id": "uniswap-v2", "address": "0x" + "44" * 20,
            "deployment_block": 1, "expected_code_hash": "11" * 32,
        }]),
        base_liquidity_lockers_json=json.dumps([{
            "amm_id": "uniswap-v2", "address": "0x" + "55" * 20,
            "deployment_block": 1, "expected_code_hash": "11" * 32,
            "abi_profile": "uncx-v2.1-locks-nonce",
        }]),
        base_liquidity_registry_complete=True,
    )
    with pytest.raises(ValueError, match="conflicts with pinned registry identity"):
        build_liquidity_snapshot_providers(configured, chains=("base",))

def test_sell_simulation_factory_is_disabled_by_default_and_requires_explicit_test_cases(tmp_path):
    from veltrix.providers.factory import build_solana_sell_simulation_worker
    from veltrix.solana_meme.sell_simulation_store import SolanaSellSimulationStore

    store = SolanaSellSimulationStore(str(tmp_path / "factory.sqlite"))
    assert build_solana_sell_simulation_worker(
        settings(), str(tmp_path / "factory.sqlite"), store,
    ) is None
    enabled_but_incomplete = settings(
        solana_sell_simulation_enabled=True,
        solana_sell_simulation_taker="",
        solana_sell_simulation_cases_json="{}",
        solana_rpc_url="https://rpc.example",
    )
    assert build_solana_sell_simulation_worker(
        enabled_but_incomplete, str(tmp_path / "factory.sqlite"), store,
    ) is None


def test_sell_simulation_factory_builds_only_from_public_taker_and_explicit_amounts(tmp_path):
    import json
    from veltrix.providers.factory import build_solana_sell_simulation_worker
    from veltrix.solana_meme.sell_simulation_store import SolanaSellSimulationStore
    from veltrix.solana_meme.simulation_preflight_store import SolanaSimulationPreflightStore
    from veltrix.solana_meme.sell_simulation_worker import SolanaSellSimulationWorker

    db = str(tmp_path / "factory.sqlite")
    store = SolanaSellSimulationStore(db)
    preflight_store = SolanaSimulationPreflightStore(db)
    configured = settings(
        solana_sell_simulation_enabled=True,
        solana_sell_simulation_taker="11111111111111111111111111111111",
        solana_sell_simulation_cases_json=json.dumps({
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 1000,
        }),
        solana_rpc_url="https://rpc.example",
        jupiter_api_key="",
        jupiter_quote_min_interval_seconds=2.1,
    )
    worker = build_solana_sell_simulation_worker(configured, db, store, preflight_store=preflight_store)
    assert isinstance(worker, SolanaSellSimulationWorker)
    assert worker.test_cases == {
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": 1000,
    }
