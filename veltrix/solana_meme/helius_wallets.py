"""Helius-backed, fail-closed wallet observations for Phase 06 consensus.

Notional is estimated with the latest persisted spot price, not historical
execution pricing. The source only scans explicitly operator-allowlisted
wallets and never discovers or promotes wallets automatically.
"""
from __future__ import annotations

import math
from typing import Callable

from ..providers.helius import HeliusWalletProvider
from ..providers.intelligence import VerifiedIntelligenceAdapters
from ..whales import WalletBuyEvent
from .market_store import MarketSnapshotStore


class HeliusConsensusWalletEventSource:
    def __init__(
        self,
        provider: HeliusWalletProvider,
        adapters: VerifiedIntelligenceAdapters,
        market_store: MarketSnapshotStore,
        *,
        clock_ms: Callable[[], int],
        max_age_ms: int = 300_000,
    ) -> None:
        if not isinstance(provider, HeliusWalletProvider):
            raise TypeError("provider must be a HeliusWalletProvider")
        if not isinstance(adapters, VerifiedIntelligenceAdapters):
            raise TypeError("adapters must be VerifiedIntelligenceAdapters")
        if not isinstance(market_store, MarketSnapshotStore):
            raise TypeError("market_store must be a MarketSnapshotStore")
        if not callable(clock_ms):
            raise ValueError("clock_ms must be callable")
        if isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int) or max_age_ms < 1:
            raise ValueError("max_age_ms must be a positive integer")
        if provider.provider_id not in adapters.trusted_wallet_providers:
            raise ValueError("Helius must be explicitly trusted in wallet provider settings")
        if not provider.tracked_wallets:
            raise ValueError("at least one explicitly tracked wallet is required")
        self.provider = provider
        self.adapters = adapters
        self.market_store = market_store
        self.clock_ms = clock_ms
        self.max_age_ms = max_age_ms
        # Helius observations use a current persisted spot price for an explicitly
        # labeled notional estimate; it is not historical execution value.
        self.provider.price_usd_resolver = self._spot_price

    def _spot_price(self, mint: str) -> float:
        row = self.market_store.latest(mint)
        if row is None:
            return 0.0
        value = row.get("price_usd")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        price = float(value)
        return price if math.isfinite(price) and price > 0 else 0.0

    def fetch_many(
        self, mints: tuple[str, ...] | list[str], now_ms: int | None = None
    ) -> tuple[WalletBuyEvent, ...]:
        """Fetch all allowlisted-wallet observations in one provider batch."""
        targets = tuple(dict.fromkeys(mints))
        if not targets:
            return ()
        if any(not isinstance(mint, str) or not mint.strip() for mint in targets):
            raise ValueError("wallet evidence targets must be non-empty mint strings")
        now = self.clock_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("wallet source clock must return a positive integer")
        observations = self.provider.fetch_wallet_observations(
            "solana", targets, max(0, now - self.max_age_ms)
        )
        batch = self.adapters.adapt_wallet_observations(observations, now)
        return batch.accepted

    def __call__(self, mint: str) -> tuple[WalletBuyEvent, ...]:
        return self.fetch_many((mint,))
