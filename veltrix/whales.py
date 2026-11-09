"""Allowlist-only Solana wallet-buy clustering.

The aggregator consumes normalized buy observations. It does not discover
profitable wallets, infer buys from price action, or generate trade signals.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .solana_meme.candidates import canonical_mint


@dataclass(frozen=True)
class WalletBuyEvent:
    chain: str
    token_address: str
    wallet_address: str
    observed_at_ms: int
    notional_usd: float
    source: str

    def __post_init__(self) -> None:
        if self.chain != "solana":
            raise ValueError("only Solana wallet events are supported")
        if canonical_mint(self.token_address) != self.token_address:
            raise ValueError("token address must be canonical")
        if canonical_mint(self.wallet_address) != self.wallet_address:
            raise ValueError("wallet address must be canonical")
        if isinstance(self.observed_at_ms, bool) or not isinstance(self.observed_at_ms, int) or self.observed_at_ms <= 0:
            raise ValueError("observed_at_ms must be a positive integer")
        if (
            isinstance(self.notional_usd, bool)
            or not isinstance(self.notional_usd, (int, float))
            or not math.isfinite(float(self.notional_usd))
            or self.notional_usd <= 0
        ):
            raise ValueError("notional_usd must be finite and positive")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source identity is required")


@dataclass(frozen=True)
class WhaleCluster:
    chain: str
    token_address: str
    wallet_addresses: tuple[str, ...]
    total_notional_usd: float
    first_seen_ms: int
    last_seen_ms: int


class WhaleFlowAggregator:
    def __init__(
        self,
        trusted_wallets: Iterable[str] = (),
        window_ms: int = 300_000,
        min_buy_usd: float = 0.0,
    ):
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

    def aggregate(self, events: Iterable[WalletBuyEvent], now_ms: int) -> list[WhaleCluster]:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
            raise ValueError("now_ms must be a positive integer")
        grouped: dict[tuple[str, str], list[WalletBuyEvent]] = {}
        seen_events: set[tuple[str, str, int, float, str]] = set()
        for event in events:
            if not isinstance(event, WalletBuyEvent):
                continue
            age = now_ms - event.observed_at_ms
            if (
                event.chain != "solana"
                or event.wallet_address not in self.trusted_wallets
                or age < 0
                or age > self.window_ms
                or event.notional_usd < self.min_buy_usd
            ):
                continue
            dedupe_key = (
                event.token_address,
                event.wallet_address,
                event.observed_at_ms,
                float(event.notional_usd),
                event.source,
            )
            if dedupe_key in seen_events:
                continue
            seen_events.add(dedupe_key)
            grouped.setdefault((event.chain, event.token_address), []).append(event)

        clusters = []
        for (chain, token), token_events in grouped.items():
            wallets = sorted({event.wallet_address for event in token_events})
            if len(wallets) < 2:
                continue
            clusters.append(WhaleCluster(
                chain=chain,
                token_address=token,
                wallet_addresses=tuple(wallets),
                total_notional_usd=sum(event.notional_usd for event in token_events),
                first_seen_ms=min(event.observed_at_ms for event in token_events),
                last_seen_ms=max(event.observed_at_ms for event in token_events),
            ))
        return sorted(clusters, key=lambda cluster: cluster.token_address)
