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
    root_provider = None
    root_url = getattr(settings, "developer_root_attestation_url", "")
    root_key = getattr(settings, "developer_root_attestation_public_key", "")
    if root_url and root_key:
        from .signed_roots import SignedDeveloperRootProvider
        root_provider = SignedDeveloperRootProvider(
            root_url, root_key,
            provider_id=getattr(settings, "developer_root_attestation_provider_id", "signed-developer-roots"),
            max_age_ms=getattr(settings, "safety_evidence_max_age_ms", 300_000),
        )
    return DeveloperEvidenceOrchestrator(
        holder_providers=holders, funding_providers=funding,
        analyzer=DeveloperConcentrationAnalyzer(
            max_age_ms=getattr(settings, "safety_evidence_max_age_ms", 300_000)
        ), intelligence_adapters=intelligence_adapters,
        trusted_root_providers=getattr(settings, "trusted_developer_root_providers", ()),
        trusted_holder_providers=getattr(settings, "trusted_holder_inventory_providers", ()),
        trusted_funding_providers=getattr(settings, "trusted_developer_funding_providers", ()),
        root_attestation_provider=root_provider,
        # No production capability is currently certified for this composite graph.
        certified_provider_id=None, audit_sink=audit_sink,
    )



def build_liquidity_snapshot_providers(settings, *, session=None, chains=("base", "bsc", "solana")):
    """Construct optional read-only chain snapshot readers.

    These readers are deliberately not registered as certified capabilities.
    EVM factories and locker bytecode hashes must be operator-pinned; registry
    completeness is an explicit opt-in attestation. Solana is limited to the
    published UNCX Raydium AMM V4 program profile.
    """
    import json
    from .evm_liquidity_reader import (
        EvmFactorySpec, EvmLockerSpec, EvmV2LiquiditySnapshotReader,
    )
    from ..base_factory_registry import (
        BASE_V2_FACTORY_CANDIDATES, BASE_V2_FACTORY_REGISTRY_COMPLETE,
    )
    from .solana_liquidity_reader import SolanaRaydiumV4CpmmSnapshotReader

    allowed = {"base", "bsc", "solana"}
    if not set(chains).issubset(allowed):
        raise ValueError("unsupported liquidity reader chain")
    providers = {}

    def parse_registry(raw, model, label):
        if not raw:
            return ()
        try:
            rows = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be valid JSON") from exc
        if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{label} must be a non-empty array of JSON objects")
        try:
            return tuple(model(**row) for row in rows)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} contains an invalid entry") from exc

    for chain in ("base", "bsc"):
        if chain not in chains:
            continue
        pool_url = getattr(settings, f"{chain}_liquidity_pool_rpc_url", "")
        locker_url = getattr(settings, f"{chain}_liquidity_locker_rpc_url", "")
        factories_raw = getattr(settings, f"{chain}_liquidity_factories_json", "")
        lockers_raw = getattr(settings, f"{chain}_liquidity_lockers_json", "")
        complete = getattr(settings, f"{chain}_liquidity_registry_complete", False)
        if not any((pool_url, locker_url, factories_raw, lockers_raw, complete)):
            continue
        factories = parse_registry(
            factories_raw, EvmFactorySpec, f"{chain.upper()}_LIQUIDITY_AMM_FACTORIES_JSON"
        )
        if chain == "base":
            # Always inject the source-pinned candidate and reject attempts to
            # override its identity. This does not certify registry completeness.
            pinned = {item.amm_id: item.as_reader_spec()
                      for item in BASE_V2_FACTORY_CANDIDATES}
            configured = {item.amm_id: item for item in factories}
            for amm_id, spec in pinned.items():
                supplied = configured.get(amm_id)
                if supplied is not None and (
                    supplied.address.lower() != spec.address.lower()
                    or supplied.deployment_block != spec.deployment_block
                    or supplied.expected_code_hash.lower() != spec.expected_code_hash.lower()
                ):
                    raise ValueError("configured Base factory conflicts with pinned registry identity")
                configured[amm_id] = spec
            factories = tuple(configured.values())
            complete = bool(complete) and BASE_V2_FACTORY_REGISTRY_COMPLETE
        lockers = parse_registry(
            lockers_raw, EvmLockerSpec, f"{chain.upper()}_UNCX_LOCKERS_JSON"
        )
        providers[chain] = EvmV2LiquiditySnapshotReader(
            chain, pool_url, locker_url, factories, lockers,
            registry_complete=complete, session=session,
            block_chunk_size=getattr(settings, "launch_block_chunk_size", 2_000),
            max_logs=getattr(settings, "liquidity_reader_max_logs", 200_000),
            max_pools=getattr(settings, "liquidity_reader_max_pools", 50_000),
            max_locks=getattr(settings, "liquidity_reader_max_locks", 100_000),
        )

    sol_pool = getattr(settings, "solana_liquidity_pool_rpc_url", "")
    sol_locker = getattr(settings, "solana_liquidity_locker_rpc_url", "")
    if "solana" in chains and (sol_pool or sol_locker):
        providers["solana"] = SolanaRaydiumV4CpmmSnapshotReader(
            sol_pool, sol_locker, session=session,
            expected_raydium_executable_hash=getattr(settings, "solana_raydium_amm_v4_executable_hash", ""),
            expected_raydium_cpmm_executable_hash=getattr(settings, "solana_raydium_cpmm_executable_hash", ""),
            registry_complete=getattr(settings, "solana_liquidity_registry_complete", False),
            max_pools=getattr(settings, "liquidity_reader_max_pools", 20_000),
            max_locks_per_pool=getattr(settings, "liquidity_reader_max_locks", 20_000),
        )
    return providers


def build_solana_risk_evidence_collector(settings, *, session=None):
    """Build the optional GoPlus + holder + finalized mint evidence collector.

    Existing GOPLUS_ACCESS_TOKEN and SOLANA_RPC_URL settings are reused. No
    provider is added to a trust allowlist and no missing credential is guessed.
    """
    goplus_token = getattr(settings, "goplus_access_token", "")
    solana_rpc = getattr(settings, "solana_rpc_url", "")
    if not goplus_token or not solana_rpc:
        return None

    from .goplus import GoPlusTokenSafetyProvider
    from .holder_inventory import SolanaHolderInventoryProvider
    from .solana_mint import SolanaMintAuthorityProvider
    from ..solana_meme.risk_evidence import SolanaRiskEvidenceCollector

    max_age = getattr(settings, "safety_evidence_max_age_ms", 300_000)
    goplus = GoPlusTokenSafetyProvider(access_token=goplus_token, session=session)
    holders = SolanaHolderInventoryProvider(
        solana_rpc, session=session,
        max_accounts=getattr(settings, "holder_inventory_max_accounts", 50_000),
    )
    mint = SolanaMintAuthorityProvider(solana_rpc, session=session)
    return SolanaRiskEvidenceCollector(
        goplus, holders, mint_provider=mint, max_age_ms=max_age,
    )


def build_solana_developer_evidence_worker(settings, launch_store, evidence_store, *, session=None):
    """Build developer ancestry collection only when all providers are explicitly trusted.

    Reuses existing Helius/Solana RPC credentials and signed-root configuration.
    This worker never receives a certified provider ID.
    """
    from ..solana_meme.developer_worker import SolanaDeveloperEvidenceWorker

    endpoint = getattr(settings, "developer_root_attestation_url", "")
    public_key = getattr(settings, "developer_root_attestation_public_key", "")
    provider_id = getattr(settings, "developer_root_attestation_provider_id", "signed-developer-roots")
    trusted_roots = getattr(settings, "trusted_developer_root_providers", ())
    trusted_holders = getattr(settings, "trusted_holder_inventory_providers", ())
    trusted_funding = getattr(settings, "trusted_developer_funding_providers", ())
    if not (endpoint and public_key and provider_id in trusted_roots
            and "solana-rpc-holder-inventory" in trusted_holders
            and "helius-solana-funding-graph" in trusted_funding
            and getattr(settings, "helius_api_key", "")
            and getattr(settings, "solana_rpc_url", "")):
        return None

    from .signed_roots import SignedDeveloperRootProvider

    holders = build_holder_inventory_providers(settings, session=session)
    funding = build_funding_graph_providers(settings, session=session)
    if "solana" not in holders or "solana" not in funding:
        return None
    root_provider = SignedDeveloperRootProvider(
        endpoint, public_key, provider_id=provider_id, session=session,
        max_age_ms=getattr(settings, "safety_evidence_max_age_ms", 300_000),
    )
    return SolanaDeveloperEvidenceWorker(
        launch_store, holders["solana"], funding["solana"], root_provider,
        evidence_store,
        trusted_holder_providers=trusted_holders,
        trusted_funding_providers=trusted_funding,
        trusted_root_providers=trusted_roots,
        max_age_ms=getattr(settings, "safety_evidence_max_age_ms", 300_000),
        max_ancestry_depth=getattr(settings, "developer_ancestry_max_depth", 8),
    )


def build_solana_launch_bundle_worker(settings, launch_store, evidence_store, *, session=None):
    """Build Pump.fun launch-bundle evidence only with explicit provider trust.

    The report remains heuristic and is not registered as a certified safety claim.
    """
    required_launch = "helius-launch-origin"
    required_bundle = "helius-solana-launch-index"
    required_funding = "helius-solana-funding-graph"
    trusted_launch = getattr(settings, "trusted_launch_origin_providers", ())
    trusted_bundle = getattr(settings, "trusted_bundle_providers", ())
    trusted_funding = getattr(settings, "trusted_funding_providers", ())
    if not (
        getattr(settings, "helius_api_key", "")
        and getattr(settings, "solana_rpc_url", "")
        and required_launch in trusted_launch
        and required_bundle in trusted_bundle
        and required_funding in trusted_funding
    ):
        return None

    from ..launch_bundles import LaunchBundleAnalyzer
    from ..solana_meme.bundle_worker import SolanaLaunchBundleWorker
    from .helius_launch_index import HeliusSolanaLaunchIndexProvider

    funding = build_funding_graph_providers(settings, session=session)
    if "solana" not in funding:
        return None
    window = getattr(settings, "launch_window_ms", 120_000)
    provider = HeliusSolanaLaunchIndexProvider(
        settings.helius_api_key, settings.solana_rpc_url, funding["solana"],
        session=session, launch_window_ms=window,
        max_scan_pages=getattr(settings, "launch_index_max_pages", 20),
        max_buyers=min(getattr(settings, "funding_graph_max_wallets", 500), 500),
        max_scanned_transactions=10_000,
    )
    analyzer = LaunchBundleAnalyzer(
        trusted_providers=trusted_bundle,
        trusted_funder_providers=trusted_funding,
        launch_window_ms=window,
    )
    return SolanaLaunchBundleWorker(
        launch_store, provider, analyzer, evidence_store,
    )
