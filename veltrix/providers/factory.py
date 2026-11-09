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


def build_intelligence_coordinator(settings, providers=None):
    """Build the cross-provider coordinator without changing trust settings."""
    from .coordinator import CrossChainIntelligenceCoordinator
    from .intelligence import VerifiedIntelligenceAdapters

    connectors = tuple(providers) if providers is not None else build_wallet_providers(settings)
    trusted_wallets = tuple(settings.tracked_smart_wallets) + tuple(
        address.lower() for address in settings.tracked_evm_wallets
    )
    adapters = VerifiedIntelligenceAdapters.from_settings(settings)
    return CrossChainIntelligenceCoordinator(
        connectors, adapters, trusted_wallets=trusted_wallets,
    )


def build_holder_inventory_providers(settings, *, session=None):
    """Construct optional native holder inventory adapters.

    Returned adapters are read-only and deliberately are not added to the
    composite capability catalogue until production response certification.
    """
    from .holder_inventory import EvmHolderInventoryProvider, SolanaHolderInventoryProvider

    providers = {}
    solana_rpc = getattr(settings, "solana_rpc_url", "")
    if solana_rpc:
        providers["solana"] = SolanaHolderInventoryProvider(
            solana_rpc, session=session,
            max_accounts=getattr(settings, "holder_inventory_max_accounts", 50_000),
        )
    for chain, attr in (("base", "base_rpc_url"), ("bsc", "bsc_rpc_url")):
        rpc_url = getattr(settings, attr, "")
        if rpc_url:
            providers[chain] = EvmHolderInventoryProvider(
                chain, rpc_url, session=session,
                block_chunk_size=getattr(settings, "launch_block_chunk_size", 2_000),
                max_logs=getattr(settings, "holder_inventory_max_logs", 200_000),
                max_holders=getattr(settings, "holder_inventory_max_holders", 50_000),
            )
    return providers


def build_funding_graph_providers(settings, *, session=None):
    """Build only funding sources whose documented chain coverage is adequate.

    Alchemy documents internal-transfer coverage on Base, but not BSC. Helius
    Parsed Events supplies Solana native-SOL ancestry. BSC remains withheld
    rather than silently presenting partial histories as complete.
    """
    from .alchemy_funding import AlchemyBaseFundingGraphProvider
    from .helius_funding import HeliusSolanaFundingGraphProvider

    providers = {}
    if getattr(settings, "helius_api_key", ""):
        providers["solana"] = HeliusSolanaFundingGraphProvider(
            settings.helius_api_key, session=session,
            max_pages=getattr(settings, "funding_graph_max_pages", 100),
            max_wallets=getattr(settings, "funding_graph_max_wallets", 2_000),
            max_total_pages=getattr(settings, "funding_graph_max_total_pages", 1_000),
        )
    if getattr(settings, "alchemy_api_key", ""):
        providers["base"] = AlchemyBaseFundingGraphProvider(
            settings.alchemy_api_key, session=session,
            max_pages=getattr(settings, "funding_graph_max_pages", 100),
            max_wallets=getattr(settings, "funding_graph_max_wallets", 2_000),
            max_total_pages=getattr(settings, "funding_graph_max_total_pages", 1_000),
        )
    return providers


def build_developer_evidence_orchestrator(settings, intelligence_adapters, *,
                                          holder_providers=None, funding_providers=None,
                                          audit_sink=None):
    from ..developer_concentration import DeveloperConcentrationAnalyzer
    from ..developer_evidence import DeveloperEvidenceOrchestrator

    holders = holder_providers or build_holder_inventory_providers(settings)
    funding = funding_providers or build_funding_graph_providers(settings)
    return DeveloperEvidenceOrchestrator(
        holder_providers=holders, funding_providers=funding,
        analyzer=DeveloperConcentrationAnalyzer(
            max_age_ms=getattr(settings, "safety_evidence_max_age_ms", 300_000)
        ), intelligence_adapters=intelligence_adapters,
        trusted_root_providers=getattr(settings, "trusted_developer_root_providers", ()),
        trusted_holder_providers=getattr(settings, "trusted_holder_inventory_providers", ()),
        trusted_funding_providers=getattr(settings, "trusted_developer_funding_providers", ()),
        # No production capability is currently certified for this composite graph.
        certified_provider_id=None, audit_sink=audit_sink,
    )
