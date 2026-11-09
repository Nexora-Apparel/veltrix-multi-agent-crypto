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
    price_change_m5_pct: float
    observed_at_ms: int
    pair_created_at_ms: int | None
    source: str

    def is_fresh(self, now_ms: int, max_age_ms: int = 120_000) -> bool:
        return 0 <= now_ms - self.observed_at_ms <= max_age_ms
