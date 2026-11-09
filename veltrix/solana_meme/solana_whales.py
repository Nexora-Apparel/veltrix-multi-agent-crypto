"""Strict Solana-only wallet-flow aggregation for Phase 06 consensus."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from ..whales import WalletBuyEvent
from .candidates import canonical_mint


@dataclass(frozen=True)
class SolanaWhaleCluster:
    chain: str
    token_address: str
    wallet_addresses: tuple[str, ...]
    total_notional_usd: float
    first_seen_ms: int
    last_seen_ms: int


class SolanaWhaleFlowAggregator:
    def __init__(
        self,
        trusted_wallets: Iterable[str] = (),
        *,
        window_ms: int = 300_000,
        min_buy_usd: float = 100.0,
    ) -> None:
        if isinstance(window_ms, bool) or not isinstance(window_ms, int) or window_ms <= 0:
            raise ValueError("window_ms must be a positive integer")
        if (
            isinstance(min_buy_usd, bool)
            or not isinstance(min_buy_usd, (int, float))
            or not math.isfinite(float(min_buy_usd))
            or min_buy_usd < 0
        ):
            raise ValueError("min_buy_usd must be finite and non-negative")
        self.trusted_wallets = frozenset(canonical_mint(wallet) for wallet in trusted_wallets)
        self.window_ms = window_ms
        self.min_buy_usd = float(min_buy_usd)

    def aggregate(self, events: Iterable[WalletBuyEvent], now_ms: int) -> list[SolanaWhaleCluster]:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
            raise ValueError("now_ms must be a positive integer")
        grouped: dict[str, list[WalletBuyEvent]] = {}
        seen: set[tuple[str, str, int, float, str]] = set()
        for event in events:
            if not isinstance(event, WalletBuyEvent) or event.chain != "solana":
                continue
            try:
                mint = canonical_mint(event.token_address)
                wallet = canonical_mint(event.wallet_address)
            except (TypeError, ValueError):
                continue
            if (
                mint != event.token_address
                or wallet != event.wallet_address
                or wallet not in self.trusted_wallets
                or isinstance(event.observed_at_ms, bool)
                or not isinstance(event.observed_at_ms, int)
                or event.observed_at_ms <= 0
                or now_ms - event.observed_at_ms < 0
                or now_ms - event.observed_at_ms > self.window_ms
                or isinstance(event.notional_usd, bool)
                or not isinstance(event.notional_usd, (int, float))
                or not math.isfinite(float(event.notional_usd))
                or event.notional_usd < self.min_buy_usd
                or event.notional_usd <= 0
                or not isinstance(event.source, str)
                or not event.source.strip()
            ):
                continue
            key = (mint, wallet, event.observed_at_ms, float(event.notional_usd), event.source)
            if key in seen:
                continue
            seen.add(key)
            grouped.setdefault(mint, []).append(event)

        clusters = []
        for mint, observations in grouped.items():
            wallets = tuple(sorted({event.wallet_address for event in observations}))
            if len(wallets) < 2:
                continue
            clusters.append(SolanaWhaleCluster(
                chain="solana",
                token_address=mint,
                wallet_addresses=wallets,
                total_notional_usd=sum(float(event.notional_usd) for event in observations),
                first_seen_ms=min(event.observed_at_ms for event in observations),
                last_seen_ms=max(event.observed_at_ms for event in observations),
            ))
        return sorted(clusters, key=lambda item: item.token_address)
