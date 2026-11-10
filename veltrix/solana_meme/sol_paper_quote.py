"""Fresh, provenance-checked USD-to-SOL conversion for SOL paper quotes."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import time
from typing import Callable, Protocol, Sequence

from ..providers.base import MarketEvent
from .candidates import canonical_mint
from .market_store import MarketSnapshotStore

WSOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_QUOTES = {"USD", "USDC", "USDT"}


class SolUsdMarketProvider(Protocol):
    def fetch_token_pairs(self, chain: str, token_addresses: Sequence[str]) -> list[MarketEvent]:
        ...


@dataclass(frozen=True)
class SolanaPaperQuote:
    mint: str
    price_lamports: int
    token_price_usd: Decimal
    sol_price_usd: Decimal
    token_observed_at_ms: int
    sol_observed_at_ms: int
    token_source: str
    sol_source: str
    sol_usd_pair_address: str


class SolanaPaperQuoteAdapter:
    """Convert fresh token USD marks to integer lamports using fresh SOL/USD."""

    def __init__(
        self,
        market_store: MarketSnapshotStore,
        sol_usd_provider: SolUsdMarketProvider,
        *,
        max_age_ms: int = 60_000,
        min_sol_pool_liquidity_usd: float = 50_000,
        min_sol_pool_volume_h1_usd: float = 10_000,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        for name, value in (
            ("max_age_ms", max_age_ms),
            ("min_sol_pool_liquidity_usd", min_sol_pool_liquidity_usd),
            ("min_sol_pool_volume_h1_usd", min_sol_pool_volume_h1_usd),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        self.market_store = market_store
        self.sol_usd_provider = sol_usd_provider
        self.max_age_ms = int(max_age_ms)
        self.min_liquidity = float(min_sol_pool_liquidity_usd)
        self.min_volume = float(min_sol_pool_volume_h1_usd)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @staticmethod
    def _decimal(value: object, name: str) -> Decimal:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be finite and positive")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{name} must be finite and positive") from None
        if not result.is_finite() or result <= 0:
            raise ValueError(f"{name} must be finite and positive")
        return result

    def _sol_usd(self, now_ms: int) -> MarketEvent:
        events = self.sol_usd_provider.fetch_token_pairs("solana", [WSOL_MINT])
        eligible = []
        for event in events:
            if not isinstance(event, MarketEvent):
                continue
            if event.chain != "solana" or event.token_address != WSOL_MINT:
                continue
            if not isinstance(event.pair_address, str) or not event.pair_address.strip():
                continue
            quote = (event.quote_token_symbol or "").upper()
            if quote not in STABLE_QUOTES:
                continue
            if (
                isinstance(event.observed_at_ms, bool)
                or not isinstance(event.observed_at_ms, int)
                or not 0 < event.observed_at_ms <= now_ms
                or now_ms - event.observed_at_ms > self.max_age_ms
            ):
                continue
            try:
                price = self._decimal(event.price_usd, "SOL/USD price")
                liquidity = self._decimal(event.liquidity_usd, "SOL/USD liquidity")
                volume = self._decimal(event.volume_h1_usd, "SOL/USD hourly volume")
            except ValueError:
                continue
            if liquidity < Decimal(str(self.min_liquidity)) or volume < Decimal(str(self.min_volume)):
                continue
            if not isinstance(event.source, str) or not event.source.strip():
                continue
            eligible.append((liquidity, volume, event))
        if not eligible:
            raise RuntimeError("no fresh, sufficiently liquid SOL/USD pool observation")
        return max(eligible, key=lambda item: (item[0], item[1], item[2].observed_at_ms))[2]

    def quote(self, mint: str) -> SolanaPaperQuote:
        mint = canonical_mint(mint)
        now_ms = self.clock_ms()
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
            raise ValueError("clock_ms must return a positive integer")
        token = self.market_store.latest(mint)
        if token is None:
            raise RuntimeError("token USD market observation is unavailable")
        token_at = token.get("retrieved_at_ms")
        if (
            isinstance(token_at, bool) or not isinstance(token_at, int)
            or not 0 < token_at <= now_ms or now_ms - token_at > self.max_age_ms
        ):
            raise RuntimeError("token USD market observation is stale or future-dated")
        token_source = token.get("source")
        if not isinstance(token_source, str) or not token_source.strip() or token_source == "unknown":
            raise RuntimeError("token USD market observation has no verified source provenance")
        token_usd = self._decimal(token.get("price_usd"), "token USD price")
        sol_event = self._sol_usd(now_ms)
        sol_usd = self._decimal(sol_event.price_usd, "SOL/USD price")
        lamports = int(
            (token_usd / sol_usd * Decimal(1_000_000_000)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        if lamports <= 0:
            raise RuntimeError("converted token price is below one lamport")
        return SolanaPaperQuote(
            mint=mint,
            price_lamports=lamports,
            token_price_usd=token_usd,
            sol_price_usd=sol_usd,
            token_observed_at_ms=token_at,
            sol_observed_at_ms=sol_event.observed_at_ms,
            token_source=token_source,
            sol_source=sol_event.source,
            sol_usd_pair_address=sol_event.pair_address,
        )
