"""Solana-only market snapshot normalization over the existing DexScreener provider.

Market observations are descriptive inputs, never token-safety approvals or buy
signals. The adapter deliberately selects one deepest pool instead of summing
liquidity across potentially fragmented or correlated pools.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Protocol, Sequence

from .candidates import canonical_mint
from ..providers.base import MarketEvent, ProviderError
from ..providers.dexscreener import DexScreenerProvider


class MarketProvider(Protocol):
    def fetch_token_pairs(self, chain: str, token_addresses: Sequence[str]) -> list[MarketEvent]:
        ...


class MarketDataUnavailable(RuntimeError):
    """Raised when no usable Solana market snapshot can be established."""


@dataclass(frozen=True)
class SolanaMarketSnapshot:
    mint: str
    selected_pair_address: str
    symbol: str | None
    price_usd: float
    selected_pool_liquidity_usd: float
    selected_pool_volume_h1_usd: float
    price_change_m5_pct: float
    pair_count: int
    retrieved_at_ms: int
    provider_observed_at_ms: int
    pair_created_at_ms: int | None
    quality_flags: tuple[str, ...]

    @property
    def pair_age_seconds(self) -> int | None:
        if self.pair_created_at_ms is None:
            return None
        return max(0, (self.retrieved_at_ms - self.pair_created_at_ms) // 1000)


class SolanaMarketData:
    """Fetch and normalize DexScreener pair data for one validated Solana mint."""

    def __init__(
        self,
        provider: MarketProvider | None = None,
        *,
        max_observation_age_ms: int = 120_000,
        minimum_liquidity_usd: float = 0.0,
    ) -> None:
        if max_observation_age_ms <= 0:
            raise ValueError("max_observation_age_ms must be positive")
        if not math.isfinite(minimum_liquidity_usd) or minimum_liquidity_usd < 0:
            raise ValueError("minimum_liquidity_usd must be finite and non-negative")
        self.provider = provider or DexScreenerProvider()
        self.max_observation_age_ms = max_observation_age_ms
        self.minimum_liquidity_usd = minimum_liquidity_usd

    @staticmethod
    def _finite_nonnegative(value: object, field: str) -> float:
        if isinstance(value, bool):
            raise MarketDataUnavailable(f"invalid {field}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketDataUnavailable(f"invalid {field}") from exc
        if not math.isfinite(number) or number < 0:
            raise MarketDataUnavailable(f"invalid {field}")
        return number

    @staticmethod
    def _finite_signed(value: object, field: str) -> float:
        if isinstance(value, bool):
            raise MarketDataUnavailable(f"invalid {field}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketDataUnavailable(f"invalid {field}") from exc
        if not math.isfinite(number):
            raise MarketDataUnavailable(f"invalid {field}")
        return number

    def fetch(self, mint: str, *, now_ms: int | None = None) -> SolanaMarketSnapshot:
        canonical = canonical_mint(mint)
        retrieved = now_ms if now_ms is not None else int(time.time() * 1000)
        try:
            events = self.provider.fetch_token_pairs("solana", [canonical])
        except ProviderError as exc:
            raise MarketDataUnavailable(str(exc)) from exc

        eligible: list[MarketEvent] = []
        for event in events:
            if event.chain != "solana" or event.token_address != canonical:
                continue
            if not event.pair_address:
                continue
            if isinstance(event.observed_at_ms, bool) or not isinstance(event.observed_at_ms, int):
                continue
            age = retrieved - event.observed_at_ms
            if age < 0 or age > self.max_observation_age_ms:
                continue
            if self._finite_nonnegative(event.price_usd, "price_usd") <= 0:
                continue
            self._finite_nonnegative(event.liquidity_usd, "liquidity_usd")
            self._finite_nonnegative(event.volume_h1_usd, "volume_h1_usd")
            self._finite_signed(event.price_change_m5_pct, "price_change_m5_pct")
            eligible.append(event)

        if not eligible:
            raise MarketDataUnavailable(f"no fresh usable Solana pair data for {canonical}")

        selected = max(
            eligible,
            key=lambda item: (
                item.liquidity_usd,
                item.volume_h1_usd,
                item.pair_address,
            ),
        )
        flags: list[str] = []
        if selected.liquidity_usd < self.minimum_liquidity_usd:
            flags.append("below_configured_minimum_liquidity")
        if selected.liquidity_usd == 0:
            flags.append("zero_liquidity")
        if selected.volume_h1_usd == 0:
            flags.append("zero_hourly_volume")
        if selected.pair_created_at_ms is None:
            flags.append("pair_creation_time_missing")
        elif selected.pair_created_at_ms > retrieved:
            flags.append("pair_creation_time_in_future")

        return SolanaMarketSnapshot(
            mint=canonical,
            selected_pair_address=selected.pair_address,
            symbol=selected.symbol,
            price_usd=float(selected.price_usd),
            selected_pool_liquidity_usd=float(selected.liquidity_usd),
            selected_pool_volume_h1_usd=float(selected.volume_h1_usd),
            price_change_m5_pct=float(selected.price_change_m5_pct),
            pair_count=len(eligible),
            retrieved_at_ms=retrieved,
            provider_observed_at_ms=selected.observed_at_ms,
            pair_created_at_ms=selected.pair_created_at_ms,
            quality_flags=tuple(flags),
        )
