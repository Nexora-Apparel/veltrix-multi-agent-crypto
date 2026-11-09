"""Static provider-readiness inventory; never substitutes for token evidence.

This report distinguishes credentials, explicit trust policy and known connector
capabilities. A configured provider is not proof that a particular token has
complete, fresh evidence.
"""
from dataclasses import dataclass

CHAINS = ("solana", "base", "bsc")
CLAIMS = ("liquidity_lock", "developer_holdings", "launch_bundle")


@dataclass(frozen=True)
class ClaimReadiness:
    chain: str
    claim: str
    configured: bool
    trusted: bool
    capability_complete: bool
    provider_ids: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def eligible_to_attempt(self) -> bool:
        return self.configured and self.trusted and self.capability_complete and not self.blockers


@dataclass(frozen=True)
class ProviderReadinessReport:
    claims: tuple[ClaimReadiness, ...]

    @property
    def fully_ready(self) -> bool:
        return bool(self.claims) and all(item.eligible_to_attempt for item in self.claims)

    @property
    def blocked_claims(self) -> tuple[ClaimReadiness, ...]:
        return tuple(item for item in self.claims if not item.eligible_to_attempt)


def _trusted(settings, field: str) -> frozenset[str]:
    return frozenset(getattr(settings, field, ()) or ())


def build_provider_readiness(settings) -> ProviderReadinessReport:
    """Build a conservative static inventory from settings without network calls.

    GoPlus can return useful risk signals, but its partial LP-holder data and
    creator concentration do not satisfy the complete liquidity/developer
    claims. No complete developer-linked-holdings connector is currently
    implemented. Launch indexers can attempt bundle analysis only when their
    independent funding and trust configuration is present.
    """
    liquidity_trust = _trusted(settings, "trusted_liquidity_providers")
    developer_trust = _trusted(settings, "trusted_developer_providers")
    bundle_trust = _trusted(settings, "trusted_bundle_providers")
    funding_trust = _trusted(settings, "trusted_funding_providers")
    root_trust = _trusted(settings, "trusted_developer_root_providers")
    holder_trust = _trusted(settings, "trusted_holder_inventory_providers")
    developer_funding_trust = _trusted(settings, "trusted_developer_funding_providers")
    origin_trust = _trusted(settings, "trusted_launch_origin_providers")
    goplus_configured = True  # Public EVM endpoint; Solana beta is authenticated.
    helius = bool(getattr(settings, "helius_api_key", ""))
    solana_rpc = bool(getattr(settings, "solana_rpc_url", ""))
    root_url = bool(getattr(settings, "developer_root_attestation_url", ""))
    root_key = bool(getattr(settings, "developer_root_attestation_public_key", ""))
    root_provider_id = getattr(settings, "developer_root_attestation_provider_id", "signed-developer-roots")
    base_rpc = bool(getattr(settings, "base_rpc_url", ""))
    bsc_rpc = bool(getattr(settings, "bsc_rpc_url", ""))
    base_quotes = bool(getattr(settings, "alchemy_quote_tokens_base", ()))
    bsc_quotes = bool(getattr(settings, "alchemy_quote_tokens_bsc", ()))

    rows = []
    for chain in CHAINS:
        if chain == "base":
            reader_configured = all((
                getattr(settings, "base_liquidity_pool_rpc_url", ""),
                getattr(settings, "base_liquidity_locker_rpc_url", ""),
                getattr(settings, "base_liquidity_factories_json", ""),
                getattr(settings, "base_liquidity_lockers_json", ""),
                getattr(settings, "base_liquidity_registry_complete", False),
            ))
            liquidity_provider_id = "base-finalized-all-factory-liquidity"
        elif chain == "bsc":
            reader_configured = all((
                getattr(settings, "bsc_liquidity_pool_rpc_url", ""),
                getattr(settings, "bsc_liquidity_locker_rpc_url", ""),
                getattr(settings, "bsc_liquidity_factories_json", ""),
                getattr(settings, "bsc_liquidity_lockers_json", ""),
                getattr(settings, "bsc_liquidity_registry_complete", False),
            ))
            liquidity_provider_id = "bsc-finalized-all-factory-liquidity"
        else:
            solana_endpoints_configured = bool(
                getattr(settings, "solana_liquidity_pool_rpc_url", "")
                and getattr(settings, "solana_liquidity_locker_rpc_url", "")
            )
            solana_program_hash_pinned = bool(
                getattr(settings, "solana_raydium_amm_v4_executable_hash", "")
            )
            reader_configured = solana_endpoints_configured and solana_program_hash_pinned
            liquidity_provider_id = "solana-raydium-v4-finalized-pool-inventory"

        liquidity_blockers = []
        if chain in {"base", "bsc"} and not reader_configured:
            liquidity_blockers.append(
                "separate pool and locker RPC endpoints, pinned factory/locker registries and explicit complete-AMM registry attestation are required"
            )
        elif chain == "solana":
            if not solana_endpoints_configured:
                liquidity_blockers.append("separate Solana pool and locker RPC endpoints are required")
            if not solana_program_hash_pinned:
                liquidity_blockers.append("pinned Raydium AMM V4 executable SHA-256 hash is missing")
        if liquidity_provider_id not in liquidity_trust:
            liquidity_blockers.append(
                f"{liquidity_provider_id} is not explicitly trusted for the liquidity-lock claim"
            )
        liquidity_blockers.append(
            "live read-only acceptance against production finalized state has not been recorded; provider capability remains disabled"
        )
        if chain in {"base", "bsc"}:
            liquidity_blockers.append(
                f"{chain} locker runtime hash, factory event coverage, lock history and LP custody reconciliation must pass live acceptance"
            )
        else:
            liquidity_blockers.append(
                "Solana support is scoped to Raydium AMM V4; deployed UNCX executable, TokenLock PDAs and LP vault ownership must pass live acceptance"
            )
        rows.append(ClaimReadiness(
            chain, "liquidity_lock", reader_configured,
            liquidity_provider_id in liquidity_trust,
            False, (liquidity_provider_id,) if reader_configured else (),
            tuple(liquidity_blockers),
        ))

        developer_blockers = [
            "no capability-certified connector proves complete developer-linked wallet holdings",
        ]
        if not root_url or not root_key:
            developer_blockers.append("signed independent developer-root endpoint or Ed25519 public key is missing")
        if root_provider_id not in root_trust:
            developer_blockers.append(f"{root_provider_id} is not explicitly trusted for developer-root attestations")
        if chain == "solana":
            holder_id, funding_id = "solana-rpc-holder-inventory", "helius-solana-funding-graph"
            holder_configured, funding_configured = solana_rpc, helius
        elif chain == "base":
            holder_id, funding_id = "base-rpc-holder-inventory", "alchemy-base-funding-graph"
            holder_configured, funding_configured = base_rpc, bool(getattr(settings, "alchemy_api_key", ""))
        else:
            holder_id, funding_id = "bsc-rpc-holder-inventory", None
            holder_configured, funding_configured = bsc_rpc, False
        if not holder_configured:
            developer_blockers.append(f"{chain} holder inventory RPC is not configured")
        if holder_id not in holder_trust:
            developer_blockers.append(f"{holder_id} is not explicitly trusted for holder inventory")
        if funding_id is None or not funding_configured:
            developer_blockers.append(f"{chain} complete first-funder ancestry provider is unavailable")
        elif funding_id not in developer_funding_trust:
            developer_blockers.append(f"{funding_id} is not explicitly trusted for developer funding ancestry")
        if not developer_trust:
            developer_blockers.append("no provider is explicitly trusted for the developer-holdings claim")
        rows.append(ClaimReadiness(
            chain, "developer_holdings", False, bool(developer_trust), False, (),
            tuple(developer_blockers),
        ))

        if chain == "solana":
            indexer_configured = helius
            indexer_id, funder_id, origin_id = "helius-launch", "helius-funder", "helius-launch-origin"
        else:
            rpc = base_rpc if chain == "base" else bsc_rpc
            quotes = base_quotes if chain == "base" else bsc_quotes
            indexer_configured = rpc and quotes
            indexer_id = f"{chain}-rpc-launch"
            funder_id = f"{chain}-quote-funder"
            origin_id = f"{chain}-rpc-launch-origin"
        bundle_blockers = []
        if not indexer_configured:
            bundle_blockers.append("launch indexer endpoint or required quote-token configuration is missing")
        if indexer_id not in bundle_trust:
            bundle_blockers.append(f"{indexer_id} is not explicitly trusted for bundle evidence")
        if funder_id not in funding_trust:
            bundle_blockers.append(f"{funder_id} is not explicitly trusted for funding traces")
        if origin_id not in origin_trust:
            bundle_blockers.append(f"{origin_id} is not explicitly trusted for launch-origin evidence")
        rows.append(ClaimReadiness(
            chain, "launch_bundle", bool(indexer_configured), indexer_id in bundle_trust
            and funder_id in funding_trust and origin_id in origin_trust,
            bool(indexer_configured), (indexer_id, funder_id, origin_id) if indexer_configured else (),
            tuple(bundle_blockers),
        ))

    return ProviderReadinessReport(tuple(rows))
