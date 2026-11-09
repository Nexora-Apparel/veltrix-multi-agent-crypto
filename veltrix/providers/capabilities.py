"""Documented capability boundaries for independent safety evidence providers.

A provider may be useful without being capable of certifying a claim. This
catalog intentionally marks incomplete coverage as non-certifying.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderCapability:
    provider_id: str
    chain: str
    claim: str
    status: str
    documented_scope: str
    certification_blocker: str
    source_url: str

    @property
    def can_certify(self) -> bool:
        return self.status == "complete" and not self.certification_blocker


CAPABILITIES = (
    ProviderCapability(
        "goplus", "base", "liquidity_lock", "partial",
        "Token security and token-locker lookup; supported locker integrations are listed.",
        "Locker response schema and exhaustive all-pool LP-position coverage are not documented.",
        "https://docs.gopluslabs.io/reference/gettokenlockersusingget",
    ),
    ProviderCapability(
        "goplus", "bsc", "liquidity_lock", "partial",
        "Token security and token-locker lookup; supported locker integrations are listed.",
        "Locker response schema and exhaustive all-pool LP-position coverage are not documented.",
        "https://docs.gopluslabs.io/reference/gettokenlockersusingget",
    ),
    ProviderCapability(
        "goplus", "solana", "liquidity_lock", "unsupported",
        "GoPlus Solana token-security beta is not a documented LP-lock inventory.",
        "No documented Solana liquidity-lock coverage in this connector.",
        "https://docs.gopluslabs.io/reference/supported-locker",
    ),
    *(
        ProviderCapability(
            "insightx-clusters", chain, "developer_holdings", "partial",
            "Holder clusters link wallets using shared funding sources or transaction patterns; balances are recalculated from live chain data.",
            "Public docs do not establish exhaustive holder coverage, complete developer identity attribution, or a response completeness watermark.",
            "https://docs.insightx.network/reference/get_clusters_dex_metrics_v1__network___token_address__clusters_get",
        )
        for chain in ("solana", "base", "bsc")
    ),
)


def capability_for(provider_id: str, chain: str, claim: str) -> ProviderCapability | None:
    chain_key = chain.lower() if isinstance(chain, str) else ""
    return next((
        item for item in CAPABILITIES
        if item.provider_id == provider_id and item.chain == chain_key and item.claim == claim
    ), None)


def capability_can_certify(provider_id: str, chain: str, claim: str) -> bool:
    item = capability_for(provider_id, chain, claim)
    return bool(item and item.can_certify)
