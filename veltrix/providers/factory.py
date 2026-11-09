"""Construct optional read-only wallet providers from environment settings.

Credentials and wallet lists are never required for importing the application.
No provider is trusted by this factory; trust remains an explicit adapter setting.
"""
from .alchemy import AlchemyWalletProvider
from .dexscreener import DexScreenerProvider
from .helius import HeliusWalletProvider
from .pricing import DexScreenerPriceResolver


def build_wallet_providers(settings, market_provider=None):
    providers = []
    if settings.helius_api_key and settings.tracked_smart_wallets:
        dex = market_provider or DexScreenerProvider()
        providers.append(HeliusWalletProvider.from_settings(
            settings,
            price_usd_resolver=DexScreenerPriceResolver(dex, chain="solana"),
        ))
    if settings.alchemy_api_key and settings.tracked_evm_wallets:
        providers.append(AlchemyWalletProvider.from_settings(settings))
    return tuple(providers)
