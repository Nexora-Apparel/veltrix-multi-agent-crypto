"""Evidence-backed market context for the Solana Scout agent.

This adapter derives a conservative volume-spike flag from persisted,
same-pair DexScreener observations. It does not infer wallet identity or
manufacture smart-wallet BUY evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
from statistics import median
import time
from typing import Callable

from .decision_runner import SolanaAgentContext
from .candidates import canonical_mint


class SolanaMarketAgentContextProvider:
    provider_id = "veltrix-dexscreener-market-context"

    def __init__(
        self,
        market_store,
        *,
        baseline_observations: int = 5,
        volume_spike_multiple: float = 2.0,
        max_latest_age_ms: int = 120_000,
        max_baseline_age_ms: int = 900_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if isinstance(baseline_observations, bool) or not isinstance(baseline_observations, int) or baseline_observations < 3:
            raise ValueError("baseline_observations must be an integer of at least three")
        if (isinstance(volume_spike_multiple, bool)
                or not isinstance(volume_spike_multiple, (int, float))
                or not math.isfinite(volume_spike_multiple)
                or volume_spike_multiple <= 1):
            raise ValueError("volume_spike_multiple must be finite and greater than one")
        if min(max_latest_age_ms, max_baseline_age_ms) < 1:
            raise ValueError("freshness limits must be positive")
        self.market_store = market_store
        self.baseline_observations = baseline_observations
        self.volume_spike_multiple = float(volume_spike_multiple)
        self.max_latest_age_ms = max_latest_age_ms
        self.max_baseline_age_ms = max_baseline_age_ms
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def __call__(self, mint: str, evidence: dict) -> SolanaAgentContext:
        canonical = canonical_mint(mint)
        now = self.clock_ms()
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("clock must return a non-negative integer timestamp")
        history = self.market_store.history(
            canonical, limit=self.baseline_observations + 1,
        )
        if not history:
            raise ValueError("no persisted market history is available")
        latest = history[0]
        if latest.get("mint") != canonical:
            raise ValueError("latest market snapshot mint mismatch")
        latest_at = latest.get("retrieved_at_ms")
        if (isinstance(latest_at, bool) or not isinstance(latest_at, int)
                or not 0 <= now - latest_at <= self.max_latest_age_ms):
            raise ValueError("latest market snapshot is stale or future-dated")
        price_change = latest.get("price_change_m5_pct")
        if (isinstance(price_change, bool) or not isinstance(price_change, (int, float))
                or not math.isfinite(price_change)):
            raise ValueError("latest market momentum is invalid")

        # Only compare observations for the same selected pool. Pool changes
        # invalidate the baseline rather than mixing unrelated liquidity.
        baseline_rows = history[1:]
        same_pair = all(
            row.get("pair_address") == latest.get("pair_address")
            and row.get("mint") == canonical
            for row in baseline_rows
        )
        unique_observations = []
        seen = set()
        for row in baseline_rows:
            observed = row.get("provider_observed_at_ms")
            volume = row.get("volume_h1_usd")
            retrieved = row.get("retrieved_at_ms")
            if (isinstance(observed, bool) or not isinstance(observed, int)
                    or observed in seen):
                continue
            if (isinstance(retrieved, bool) or not isinstance(retrieved, int)
                    or retrieved >= latest_at
                    or not 0 <= now - retrieved <= self.max_baseline_age_ms):
                continue
            if (isinstance(volume, bool) or not isinstance(volume, (int, float))
                    or not math.isfinite(volume) or volume < 0):
                continue
            seen.add(observed)
            unique_observations.append(float(volume))
        latest_volume = latest.get("volume_h1_usd")
        if (isinstance(latest_volume, bool) or not isinstance(latest_volume, (int, float))
                or not math.isfinite(latest_volume) or latest_volume < 0):
            raise ValueError("latest hourly volume is invalid")

        volume_spike = False
        if same_pair and len(unique_observations) >= self.baseline_observations:
            baseline = median(unique_observations[:self.baseline_observations])
            volume_spike = baseline > 0 and latest_volume >= baseline * self.volume_spike_multiple

        source_rows = [latest, *baseline_rows[:self.baseline_observations]]
        source_ids = [row.get("id") for row in source_rows]
        evidence_id = "solana-market-context:" + hashlib.sha256(
            json.dumps(
                {"mint": canonical, "provider": self.provider_id, "rows": source_ids,
                 "volume_spike": volume_spike, "momentum": float(price_change)},
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return SolanaAgentContext(
            mint=canonical,
            provider=self.provider_id,
            evidence_id=evidence_id,
            observed_at_ms=latest_at,
            volume_spike=volume_spike,
            price_change_5m_pct=float(price_change),
            smart_wallet_buys=(),
        )
