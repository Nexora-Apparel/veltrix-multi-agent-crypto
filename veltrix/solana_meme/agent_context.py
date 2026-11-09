"""Combined market and trusted Helius wallet context for Solana agents."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import time

from .decision_runner import SolanaAgentContext
from .market_agent_context import SolanaMarketAgentContextProvider


class SolanaAgentContextProvider:
    """Combine same-pool market context with explicitly trusted wallet-flow evidence.

    The market component supplies Scout momentum/volume inputs. The Helius
    coordinator supplies Whale wallet identities only after its configured
    provider and wallet allowlists accept the observations. Neither component
    makes a BUY decision.
    """

    provider_id = "solana-helius-wallet-flow-context"

    def __init__(
        self,
        coordinator,
        market_store,
        *,
        max_market_age_ms: int = 120_000,
        wallet_window_ms: int = 300_000,
        volume_baseline_observations: int = 5,
        volume_spike_multiple: float = 2.0,
        clock_ms=None,
    ):
        if not getattr(coordinator, "providers", ()):
            raise ValueError("at least one trusted wallet observation provider is required")
        adapters = getattr(coordinator, "adapters", None)
        if adapters is None or "helius" not in getattr(adapters, "trusted_wallet_providers", ()):
            raise ValueError("Helius must be explicitly allowlisted for wallet observations")
        if isinstance(wallet_window_ms, bool) or not isinstance(wallet_window_ms, int) or wallet_window_ms < 1:
            raise ValueError("wallet_window_ms must be a positive integer")
        self.coordinator = coordinator
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.market_context = SolanaMarketAgentContextProvider(
            market_store,
            baseline_observations=volume_baseline_observations,
            volume_spike_multiple=volume_spike_multiple,
            max_latest_age_ms=max_market_age_ms,
            max_baseline_age_ms=900_000,
            clock_ms=self.clock_ms,
        )
        self.wallet_window_ms = wallet_window_ms

    def __call__(self, mint: str, evidence: dict) -> SolanaAgentContext:
        market_context = self.market_context(mint, evidence)
        query_now = self.clock_ms()
        if isinstance(query_now, bool) or not isinstance(query_now, int) or query_now < 0:
            raise ValueError("clock must return a non-negative integer timestamp")
        since_ms = max(0, query_now - self.wallet_window_ms)
        result = self.coordinator.collect(
            "solana", (market_context.mint,), since_ms=since_ms, now_ms=query_now,
        )
        if getattr(result, "chain", None) != "solana":
            raise ValueError("wallet intelligence result chain mismatch")
        if getattr(result, "provider_failures", ()):
            raise RuntimeError("one or more trusted wallet providers failed")
        batch = getattr(result, "batch", None)
        if batch is None or getattr(batch, "rejected", ()):
            raise RuntimeError("wallet intelligence batch contains rejected observations")
        context = result.agent_context(market_context.mint)
        wallets = tuple(sorted(set(context.get("smart_wallet_buys", ()))))
        if any(not isinstance(wallet, str) or not wallet.strip() for wallet in wallets):
            raise ValueError("wallet intelligence returned malformed wallet identities")

        accepted = getattr(batch, "accepted", ())
        evidence_material = {
            "provider": self.provider_id,
            "mint": market_context.mint,
            "market_context_id": market_context.evidence_id,
            "wallet_events": [asdict(item) for item in accepted],
            "wallets": wallets,
            "since_ms": since_ms,
            "query_now_ms": query_now,
        }
        evidence_id = "solana-agent-context:" + hashlib.sha256(
            json.dumps(
                evidence_material, sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")
        ).hexdigest()
        return SolanaAgentContext(
            mint=market_context.mint,
            provider=self.provider_id,
            evidence_id=evidence_id,
            observed_at_ms=self.clock_ms(),
            volume_spike=market_context.volume_spike,
            price_change_5m_pct=market_context.price_change_5m_pct,
            smart_wallet_buys=wallets,
        )
