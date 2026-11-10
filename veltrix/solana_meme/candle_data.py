"""Validated OHLCV candle contract for Solana token charting.

Candles are market observations, never generated from portfolio marks or synthetic
price paths. Provider identity and observation time are retained for provenance.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

INTERVAL_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "1h": 3600,
    "4h": 14400, "1d": 86400,
}


@dataclass(frozen=True)
class SolanaCandle:
    mint: str
    pair_address: str
    interval: str
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str
    observed_at_ms: int
    is_closed: bool = True

    def __post_init__(self) -> None:
        from .candidates import canonical_mint

        canonical_mint(self.mint)
        for name in ("pair_address", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if self.interval not in INTERVAL_SECONDS:
            raise ValueError("unsupported candle interval")
        for name in ("open_time_ms", "observed_at_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.open_time_ms % (INTERVAL_SECONDS[self.interval] * 1000):
            raise ValueError("open_time_ms must align to its interval boundary")
        values = {}
        for name in ("open", "high", "low", "close", "volume"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            values[name] = float(value)
        if min(values["open"], values["close"]) < values["low"]:
            raise ValueError("low must not exceed open or close")
        if max(values["open"], values["close"]) > values["high"]:
            raise ValueError("high must not be below open or close")
        if values["high"] < values["low"]:
            raise ValueError("high must be greater than or equal to low")
        if values["open"] <= 0 or values["high"] <= 0 or values["low"] <= 0 or values["close"] <= 0:
            raise ValueError("OHLC prices must be positive")
        if not isinstance(self.is_closed, bool):
            raise ValueError("is_closed must be boolean")
