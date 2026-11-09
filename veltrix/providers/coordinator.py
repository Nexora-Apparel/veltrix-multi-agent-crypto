"""Cross-provider normalization and whale-cluster coordination.

Provider observations are gathered first, identity-checked against the connector
that returned them, then normalized in one pass. This makes deduplication
cross-provider rather than per-provider. This module never opens positions.
"""
from dataclasses import dataclass
from typing import Iterable

from .intelligence import (
    AdapterBatch,
    SmartWalletObservation,
    VerifiedIntelligenceAdapters,
)
from ..whales import WhaleCluster, WhaleFlowAggregator


@dataclass(frozen=True)
class CrossChainIntelligenceResult:
    chain: str
    batch: AdapterBatch
    clusters: tuple[WhaleCluster, ...]
    provider_failures: tuple[str, ...]

    def agent_context(self, token_address: str) -> dict:
        matching = [
            cluster for cluster in self.clusters
            if cluster.token_address.lower() == token_address.lower()
        ]
        wallets = sorted({
            wallet for cluster in matching for wallet in cluster.wallet_addresses
        })
        return {
            "smart_wallet_buys": wallets,
            "whale_cluster_count": len(matching),
            "whale_cluster_notional_usd": sum(
                cluster.total_notional_usd for cluster in matching
            ),
        }


class CrossChainIntelligenceCoordinator:
    """Collect, verify, cross-deduplicate and cluster wallet evidence."""

    def __init__(
        self,
        providers: Iterable,
        adapters: VerifiedIntelligenceAdapters,
        trusted_wallets: Iterable[str] = (),
        window_ms: int = 300_000,
        min_buy_usd: float = 0.0,
    ):
        if adapters is None:
            raise ValueError("VerifiedIntelligenceAdapters is required")
        self.providers = tuple(sorted(
            providers, key=lambda item: str(getattr(item, "provider_id", ""))
        ))
        self.adapters = adapters
        self.aggregator = WhaleFlowAggregator(
            trusted_wallets=trusted_wallets,
            window_ms=window_ms,
            min_buy_usd=min_buy_usd,
        )

    def collect(
        self, chain: str, token_addresses: Iterable[str],
        since_ms: int, now_ms: int,
    ) -> CrossChainIntelligenceResult:
        normalized_chain = str(chain).lower()
        if since_ms < 0 or now_ms < since_ms:
            raise ValueError("Invalid intelligence collection time window")
        if normalized_chain not in {"solana", "base", "bsc"}:
            raise ValueError("Unsupported intelligence chain")
        tokens = tuple(dict.fromkeys(
            item for item in token_addresses
            if isinstance(item, str) and item
        ))
        observations = []
        rejected = []
        failures = []
        for provider in self.providers:
            provider_id = getattr(provider, "provider_id", None)
            if not isinstance(provider_id, str) or provider_id not in self.adapters.trusted_wallet_providers:
                rejected.append("whale: provider identity is not trusted")
                continue
            try:
                returned = tuple(provider.fetch_wallet_observations(
                    normalized_chain, tokens, since_ms
                ))
            except Exception:
                failures.append(f"{provider_id}: wallet observation request failed")
                continue
            for index, observation in enumerate(returned):
                if (not isinstance(observation, SmartWalletObservation)
                        or observation.provider != provider_id):
                    rejected.append(
                        f"{provider_id}: provider identity mismatch on observation {index}"
                    )
                    continue
                if not isinstance(observation.chain, str) or observation.chain.lower() != normalized_chain:
                    rejected.append(
                        f"{provider_id}: observation chain does not match requested chain"
                    )
                    continue
                observations.append(observation)

        # One normalization pass means duplicate tx legs from different providers
        # collide on (chain, wallet, tx hash, token), while unrelated chains and
        # distinct tokens remain separate observations.
        batch = self.adapters.adapt_wallet_observations(observations, now_ms)
        batch = AdapterBatch(batch.accepted, tuple(rejected) + batch.rejected)
        clusters = self.aggregator.aggregate(batch.accepted, now_ms)
        return CrossChainIntelligenceResult(
            chain=normalized_chain,
            batch=batch,
            clusters=tuple(clusters),
            provider_failures=tuple(failures),
        )

    @staticmethod
    def enrich_token(token: dict, result: CrossChainIntelligenceResult) -> dict:
        """Return a copy enriched with verified cluster context; never mutate input."""
        enriched = dict(token)
        address = enriched.get("address")
        if not isinstance(address, str) or not address:
            enriched["smart_wallet_buys"] = []
            enriched["whale_cluster_count"] = 0
            enriched["whale_cluster_notional_usd"] = 0.0
            return enriched
        enriched.update(result.agent_context(address))
        return enriched
