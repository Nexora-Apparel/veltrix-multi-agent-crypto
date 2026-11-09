"""Typed provider events shared by read-only market-data adapters."""
from __future__ import annotations

from dataclasses import dataclass


class ProviderError(RuntimeError):
    """Raised when provider data cannot be trusted or normalized."""


@dataclass(frozen=True)
class MarketEvent:
    chain: str
    token_address: str
    pair_address: str
    symbol: str | None
    price_usd: float
    liquidity_usd: float
    volume_h1_usd: float
    price_change_m5_pct: float | None
    observed_at_ms: int
    pair_created_at_ms: int | None
    source: str
    name: str | None = None
    fdv_usd: float | None = None
    market_cap_usd: float | None = None
    volume_m5_usd: float | None = None
    volume_h24_usd: float | None = None
    price_change_h1_pct: float | None = None
    price_change_h24_pct: float | None = None
    txns_m5_buys: int | None = None
    txns_m5_sells: int | None = None
    txns_h1_buys: int | None = None
    txns_h1_sells: int | None = None
    txns_h24_buys: int | None = None
    txns_h24_sells: int | None = None
    dex_id: str | None = None
    quote_token_symbol: str | None = None
    pool_token_side: str | None = None

    def is_fresh(self, now_ms: int, max_age_ms: int = 120_000) -> bool:
        return (
            not isinstance(now_ms, bool)
            and isinstance(now_ms, int)
            and not isinstance(self.observed_at_ms, bool)
            and isinstance(self.observed_at_ms, int)
            and 0 <= now_ms - self.observed_at_ms <= max_age_ms
        )
