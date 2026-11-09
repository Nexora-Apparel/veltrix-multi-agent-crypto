from types import SimpleNamespace

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
