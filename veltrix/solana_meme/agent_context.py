"""Read-only Solana market and trusted-wallet context for independent agents."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import time

from .decision_runner import SolanaAgentContext


class SolanaAgentContextProvider:
    """Build typed agent inputs from fresh market history and trusted wallet flows.

    The provider never labels arbitrary wallets as smart. Wallet observations
    pass through the existing configured intelligence coordinator and its
    explicit wallet/provider allowlists. It does not emit BUY decisions.
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
        market_path = getattr(market_store, "database_path", None)
        if not isinstance(market_path, str) or not market_path.strip() or market_path == ":memory:":
            raise ValueError("a persistent market snapshot store is required")
        if min(max_market_age_ms, wallet_window_ms, volume_baseline_observations) < 1:
            raise ValueError("market, wallet and baseline bounds must be positive")
        if (isinstance(volume_spike_multiple, bool)
                or not isinstance(volume_spike_multiple, (int, float))
                or not math.isfinite(volume_spike_multiple)
                or volume_spike_multiple <= 1):
            raise ValueError("volume_spike_multiple must be finite and greater than one")
        self.coordinator = coordinator
        self.market_store = market_store
        self.max_market_age_ms = max_market_age_ms
        self.wallet_window_ms = wallet_window_ms
        self.volume_baseline_observations = volume_baseline_observations
        self.volume_spike_multiple = float(volume_spike_multiple)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @staticmethod
    def _median(values):
        ordered = sorted(values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    def __call__(self, mint: str, evidence: dict) -> SolanaAgentContext:
        from .candidates import canonical_mint

        canonical = canonical_mint(mint)
        market = evidence.get("market") if isinstance(evidence, dict) else None
        if not isinstance(market, dict):
            raise ValueError("persisted market row is required for agent context")
        if market.get("mint") != canonical:
            raise ValueError("agent market context mint mismatch")
        observed = market.get("retrieved_at_ms")
        price_change = market.get("price_change_m5_pct")
        current_volume = market.get("volume_h1_usd")
        if (isinstance(observed, bool) or not isinstance(observed, int)
                or isinstance(price_change, bool)
                or not isinstance(price_change, (int, float))
                or not math.isfinite(price_change)
                or isinstance(current_volume, bool)
                or not isinstance(current_volume, (int, float))
                or not math.isfinite(current_volume) or current_volume < 0):
            raise ValueError("market agent inputs are malformed")

        evaluation_now = evidence.get("evaluation_now_ms")
        if isinstance(evaluation_now, bool) or not isinstance(evaluation_now, int):
            evaluation_now = self.clock_ms()
        if not 0 <= evaluation_now - observed <= self.max_market_age_ms:
            raise ValueError("market agent inputs are stale or future-dated")

        history = self.market_store.history(
            canonical, limit=self.volume_baseline_observations + 1
        )
        if not history or history[0].get("id") != market.get("id"):
            raise ValueError("market history head does not match the supplied snapshot")
        baseline_rows = history[1:self.volume_baseline_observations + 1]
        baseline = []
        if len(baseline_rows) == self.volume_baseline_observations:
            for row in baseline_rows:
                volume = row.get("volume_h1_usd")
                baseline_time = row.get("retrieved_at_ms")
                if (isinstance(volume, bool) or not isinstance(volume, (int, float))
                        or not math.isfinite(volume) or volume <= 0
                        or isinstance(baseline_time, bool) or not isinstance(baseline_time, int)
                        or baseline_time >= observed
                        or not 0 <= evaluation_now - baseline_time <= 900_000):
                    baseline = []
                    break
                baseline.append(float(volume))
        volume_spike = bool(
            len(baseline) == self.volume_baseline_observations
            and self._median(baseline) > 0
            and float(current_volume) >= self._median(baseline) * self.volume_spike_multiple
        )

        query_now = self.clock_ms()
        since_ms = max(0, query_now - self.wallet_window_ms)
        result = self.coordinator.collect(
            "solana", (canonical,), since_ms=since_ms, now_ms=query_now,
        )
        if getattr(result, "chain", None) != "solana":
            raise ValueError("wallet intelligence result chain mismatch")
        if getattr(result, "provider_failures", ()):
            raise RuntimeError("one or more trusted wallet providers failed")
        batch = getattr(result, "batch", None)
        if batch is None or getattr(batch, "rejected", ()):
            raise RuntimeError("wallet intelligence batch contains rejected observations")
        context = result.agent_context(canonical)
        wallets = tuple(sorted(set(context.get("smart_wallet_buys", ()))))
        if any(not isinstance(wallet, str) or not wallet.strip() for wallet in wallets):
            raise ValueError("wallet intelligence returned malformed wallet identities")

        accepted = getattr(batch, "accepted", ())
        evidence_material = {
            "provider": self.provider_id,
            "mint": canonical,
            "market_row_id": market.get("id"),
            "market_observed_at_ms": observed,
            "wallet_events": [asdict(item) for item in accepted],
            "wallets": wallets,
            "since_ms": since_ms,
            "query_now_ms": query_now,
        }
        evidence_id = "solana-agent-context:" + hashlib.sha256(
            json.dumps(evidence_material, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return SolanaAgentContext(
            mint=canonical,
            provider=self.provider_id,
            evidence_id=evidence_id,
            observed_at_ms=self.clock_ms(),
            volume_spike=volume_spike,
            price_change_5m_pct=float(price_change),
            smart_wallet_buys=wallets,
        )
