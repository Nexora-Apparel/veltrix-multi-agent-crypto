"""Batch Helius wallet-buy ingestion for Solana consensus.

Only explicitly allowlisted wallets and the explicitly trusted Helius provider
are accepted. USD notional is an estimate using a fresh persisted spot snapshot,
not historical execution pricing.
"""
from __future__ import annotations

import math
import time
from typing import Callable, Iterable

from ..providers.base import ProviderError
from ..providers.helius import HeliusWalletProvider
from ..providers.intelligence import VerifiedIntelligenceAdapters
from ..whales import WalletBuyEvent
from .market_store import MarketSnapshotStore


class StoredMarketSpotPriceResolver:
    """Resolve current spot only from a fresh persisted Phase 04 observation."""

    def __init__(
        self,
        market_store: MarketSnapshotStore,
        *,
        clock_ms: Callable[[], int] | None = None,
        max_age_ms: int = 120_000,
    ) -> None:
        if isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int) or max_age_ms < 1:
            raise ValueError("max_age_ms must be a positive integer")
        self.market_store = market_store
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.max_age_ms = max_age_ms

    def __call__(self, mint: str) -> float:
        row = self.market_store.latest(mint)
        if row is None:
            raise ProviderError("no persisted market snapshot is available for wallet notional")
        now = self.clock_ms()
        observed = row.get("retrieved_at_ms")
        price = row.get("price_usd")
        if (
            isinstance(observed, bool)
            or not isinstance(observed, int)
            or observed <= 0
            or now < observed
            or now - observed > self.max_age_ms
        ):
            raise ProviderError("persisted spot-price snapshot is stale or future-dated")
        if (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(float(price))
            or price <= 0
        ):
            raise ProviderError("persisted spot-price snapshot is invalid")
        return float(price)


class HeliusWalletEventBatchProvider:
    """Fetch one bounded Helius wallet scan for a batch of candidate mints."""

    def __init__(
        self,
        *,
        api_key: str,
        trusted_wallets: Iterable[str],
        trusted_provider_ids: Iterable[str],
        market_store: MarketSnapshotStore,
        clock_ms: Callable[[], int] | None = None,
        max_wallet_age_ms: int = 300_000,
        price_max_age_ms: int = 120_000,
        max_tracked_wallets: int = 25,
        provider: HeliusWalletProvider | None = None,
    ) -> None:
        wallets = tuple(dict.fromkeys(wallet for wallet in trusted_wallets if isinstance(wallet, str) and wallet))
        providers = frozenset(trusted_provider_ids)
        if not api_key:
            raise ValueError("HELIUS_API_KEY is required")
        if not wallets:
            raise ValueError("at least one explicitly trusted Solana wallet is required")
        if isinstance(max_tracked_wallets, bool) or not isinstance(max_tracked_wallets, int) or max_tracked_wallets < 1:
            raise ValueError("max_tracked_wallets must be a positive integer")
        if len(wallets) > max_tracked_wallets:
            raise ValueError("allowlist exceeds the configured Helius scan limit")
        if "helius" not in providers:
            raise ValueError("helius must be explicitly listed in TRUSTED_WALLET_PROVIDERS")
        if isinstance(max_wallet_age_ms, bool) or not isinstance(max_wallet_age_ms, int) or max_wallet_age_ms < 1:
            raise ValueError("max_wallet_age_ms must be a positive integer")
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.max_wallet_age_ms = max_wallet_age_ms
        self.max_age_ms = max_wallet_age_ms
        self.provider = provider or HeliusWalletProvider(
            api_key=api_key,
            tracked_wallets=wallets,
            price_usd_resolver=StoredMarketSpotPriceResolver(
                market_store,
                clock_ms=self.clock_ms,
                max_age_ms=price_max_age_ms,
            ),
            clock_ms=self.clock_ms,
        )
        self.adapters = VerifiedIntelligenceAdapters(
            trusted_wallet_providers=providers,
            max_wallet_age_ms=max_wallet_age_ms,
        )

    def __call__(self, token_addresses: Iterable[str] | str, now_ms: int | None = None) -> tuple[WalletBuyEvent, ...]:
        now = self.clock_ms() if now_ms is None else now_ms
        if isinstance(now, bool) or not isinstance(now, int) or now <= 0:
            raise ValueError("now_ms must be a positive integer")
        values = (token_addresses,) if isinstance(token_addresses, str) else token_addresses
        targets = tuple(dict.fromkeys(token for token in values if isinstance(token, str) and token))
        if not targets:
            return ()
        batch = self.adapters.collect_wallet_observations(
            self.provider,
            "solana",
            targets,
            max(0, now - self.max_wallet_age_ms),
            now,
        )
        return batch.accepted
