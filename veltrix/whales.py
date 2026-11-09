"""Deterministic smart-wallet cluster aggregation.

Wallets are 'smart' only when explicitly allowlisted by the operator.
This module consumes normalized buy observations; it does not claim to
discover profitable wallets or infer buys from price movements.
"""
from dataclasses import dataclass
from typing import Iterable

@dataclass(frozen=True)
class WalletBuyEvent:
    chain: str
    token_address: str
    wallet_address: str
    observed_at_ms: int
    notional_usd: float
    source: str

@dataclass(frozen=True)
class WhaleCluster:
    chain: str
    token_address: str
    wallet_addresses: tuple[str, ...]
    total_notional_usd: float
    first_seen_ms: int
    last_seen_ms: int

class WhaleFlowAggregator:
    def __init__(self, trusted_wallets: Iterable[str] = (), window_ms: int = 300_000, min_buy_usd: float = 0.0):
        self.trusted_wallets = frozenset(trusted_wallets)
        self.window_ms, self.min_buy_usd = window_ms, min_buy_usd
        if window_ms <= 0 or min_buy_usd < 0:
            raise ValueError("Invalid whale aggregation limits")

    def aggregate(self, events: Iterable[WalletBuyEvent], now_ms: int) -> list[WhaleCluster]:
        grouped = {}
        for event in events:
            age = now_ms - event.observed_at_ms
            if (event.wallet_address not in self.trusted_wallets or age < 0 or age > self.window_ms
                    or event.notional_usd < self.min_buy_usd or event.notional_usd <= 0
                    or not event.token_address or not event.source):
                continue
            key = (event.chain, event.token_address)
            grouped.setdefault(key, {})[event.wallet_address] = event
        clusters = []
        for (chain, token), wallet_events in grouped.items():
            if len(wallet_events) < 2:
                continue
            ordered = sorted(wallet_events.values(), key=lambda e: e.observed_at_ms)
            clusters.append(WhaleCluster(chain, token, tuple(e.wallet_address for e in ordered),
                sum(e.notional_usd for e in ordered), ordered[0].observed_at_ms, ordered[-1].observed_at_ms))
        return clusters
