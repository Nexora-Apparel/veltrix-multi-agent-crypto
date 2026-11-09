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
    origin_trust = _trusted(settings, "trusted_launch_origin_providers")
    goplus_configured = True  # Public EVM endpoint; Solana beta is authenticated.
    helius = bool(getattr(settings, "helius_api_key", ""))
    base_rpc = bool(getattr(settings, "base_rpc_url", ""))
    bsc_rpc = bool(getattr(settings, "bsc_rpc_url", ""))
    base_quotes = bool(getattr(settings, "alchemy_quote_tokens_base", ()))
    bsc_quotes = bool(getattr(settings, "alchemy_quote_tokens_bsc", ()))

    rows = []
    for chain in CHAINS:
        # GoPlus public EVM endpoint is configured without a key; Solana beta
        # requires its access token. Its connector still cannot certify complete LP coverage.
        liquidity_configured = goplus_configured if chain in {"base", "bsc"} else bool(
            getattr(settings, "goplus_access_token", "")
        )
        liquidity_blockers = []
        if not liquidity_configured:
            liquidity_blockers.append("no configured liquidity evidence connector")
        if "goplus" not in liquidity_trust:
            liquidity_blockers.append("goplus is not explicitly trusted for liquidity")
        liquidity_blockers.append(
            "GoPlus response coverage does not prove every LP position is locked"
        )
        rows.append(ClaimReadiness(
            chain, "liquidity_lock", liquidity_configured, "goplus" in liquidity_trust,
            False, ("goplus",) if liquidity_configured else (), tuple(liquidity_blockers),
        ))

        rows.append(ClaimReadiness(
            chain, "developer_holdings", False, bool(developer_trust), False, (),
            ("no connector proves complete developer-linked wallet holdings",),
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
