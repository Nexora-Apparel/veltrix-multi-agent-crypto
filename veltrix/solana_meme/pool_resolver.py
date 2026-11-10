"""Fail-closed Solana pool resolution for the real OHLCV chart."""
from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Any

from .candidates import canonical_mint
from .market_data import MarketDataUnavailable, SolanaMarketData


class PoolResolutionUnavailable(RuntimeError):
    """No sufficiently liquid, fresh Solana pool could be established."""


@dataclass(frozen=True)
class ResolvedSolanaPool:
    mint: str
    pair_address: str
    dex_id: str | None
    quote_token_symbol: str | None
    price_usd: float
    liquidity_usd: float
    volume_h1_usd: float
    pair_count: int
    pair_created_at_ms: int | None
    observed_at_ms: int
    selection_policy: str = "highest_liquidity_then_hourly_volume"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mint": self.mint,
            "pair_address": self.pair_address,
            "dex_id": self.dex_id,
            "quote_token_symbol": self.quote_token_symbol,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "volume_h1_usd": self.volume_h1_usd,
            "pair_count": self.pair_count,
            "pair_created_at_ms": self.pair_created_at_ms,
            "observed_at_ms": self.observed_at_ms,
            "selection_policy": self.selection_policy,
            "source": "dexscreener",
            "synthetic": False,
        }


class SolanaPoolResolver:
    """Resolve a mint through fresh DexScreener pair observations.

    The resolver never guesses a pool address. It rejects low-liquidity,
    zero-volume, stale, future-dated, or invalid market observations.
    """

    def __init__(
        self,
        market_data: SolanaMarketData | None = None,
        *,
        minimum_liquidity_usd: float | None = None,
    ) -> None:
        minimum = (
            float(os.getenv("VELTRIX_CHART_MIN_POOL_LIQUIDITY_USD", "1000"))
            if minimum_liquidity_usd is None else minimum_liquidity_usd
        )
        if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(minimum) or minimum < 0:
            raise ValueError("minimum_liquidity_usd must be finite and non-negative")
        self.minimum_liquidity_usd = float(minimum)
        self.market_data = market_data or SolanaMarketData(
            minimum_liquidity_usd=self.minimum_liquidity_usd,
        )

    def resolve(self, mint: str, *, now_ms: int | None = None) -> ResolvedSolanaPool:
        canonical = canonical_mint(mint)
        try:
            snapshot = self.market_data.fetch(canonical, now_ms=now_ms)
        except (MarketDataUnavailable, ValueError) as exc:
            raise PoolResolutionUnavailable(str(exc)) from exc

        if snapshot.selected_pool_liquidity_usd < self.minimum_liquidity_usd:
            raise PoolResolutionUnavailable("best Solana pool is below the configured liquidity threshold")
        if snapshot.selected_pool_volume_h1_usd <= 0:
            raise PoolResolutionUnavailable("best Solana pool has no observed hourly volume")
        if snapshot.price_usd <= 0:
            raise PoolResolutionUnavailable("best Solana pool has no valid USD price")
        if "pair_creation_time_in_future" in snapshot.quality_flags:
            raise PoolResolutionUnavailable("pool creation timestamp is in the future")

        return ResolvedSolanaPool(
            mint=canonical,
            pair_address=snapshot.selected_pair_address,
            dex_id=snapshot.dex_id,
            quote_token_symbol=snapshot.quote_token_symbol,
            price_usd=snapshot.price_usd,
            liquidity_usd=snapshot.selected_pool_liquidity_usd,
            volume_h1_usd=snapshot.selected_pool_volume_h1_usd,
            pair_count=snapshot.pair_count,
            pair_created_at_ms=snapshot.pair_created_at_ms,
            observed_at_ms=snapshot.response_received_at_ms,
        )
