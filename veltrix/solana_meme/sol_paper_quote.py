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
class SolUsdObservation:
    price_usd: Decimal
    observed_at_ms: int
    source: str
    pair_address: str
    method: str


@dataclass(frozen=True)
class SolanaPaperQuote:
    mint: str
    market_snapshot_id: int
    token_pair_address: str
    price_lamports: int
    token_price_usd: Decimal
    sol_price_usd: Decimal
    token_observed_at_ms: int
    sol_observed_at_ms: int
    token_source: str
    sol_source: str
    sol_usd_pair_address: str
    sol_usd_method: str


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

    def _valid_event(self, event: object, now_ms: int) -> bool:
        if not isinstance(event, MarketEvent):
            return False
        if not isinstance(event.pair_address, str) or not event.pair_address.strip():
            return False
        if (
            isinstance(event.observed_at_ms, bool)
            or not isinstance(event.observed_at_ms, int)
            or not 0 < event.observed_at_ms <= now_ms
            or now_ms - event.observed_at_ms > self.max_age_ms
        ):
            return False
        if not isinstance(event.source, str) or not event.source.strip():
            return False
        try:
            liquidity = self._decimal(event.liquidity_usd, "pool liquidity")
            volume = self._decimal(event.volume_h1_usd, "pool hourly volume")
        except ValueError:
            return False
        return (
            liquidity >= Decimal(str(self.min_liquidity))
            and volume >= Decimal(str(self.min_volume))
        )

    def _sol_usd(self, now_ms: int, token_mint: str) -> SolUsdObservation:
        # Preferred source: a direct WSOL pool quoted in a stable asset.
        direct_events = []
        try:
            direct_events = self.sol_usd_provider.fetch_token_pairs("solana", [WSOL_MINT])
        except Exception:
            direct_events = []
        eligible = []
        for event in direct_events:
            if not self._valid_event(event, now_ms):
                continue
            if event.chain != "solana" or event.token_address != WSOL_MINT:
                continue
            if (event.quote_token_symbol or "").upper() not in STABLE_QUOTES:
                continue
            try:
                price = self._decimal(event.price_usd, "SOL/USD price")
                liquidity = self._decimal(event.liquidity_usd, "SOL/USD liquidity")
                volume = self._decimal(event.volume_h1_usd, "SOL/USD hourly volume")
            except ValueError:
                continue
            eligible.append((liquidity, volume, event, price))
        if eligible:
            _, _, event, price = max(
                eligible, key=lambda item: (item[0], item[1], item[2].observed_at_ms)
            )
            return SolUsdObservation(
                price, event.observed_at_ms, event.source, event.pair_address,
                "wsol_stable_pool",
            )

        # Fallback: derive SOL/USD from a fresh token/SOL pool where DexScreener
        # supplies priceUsd and priceNative for the same exact observation.
        if token_mint != WSOL_MINT:
            try:
                token_events = self.sol_usd_provider.fetch_token_pairs("solana", [token_mint])
            except Exception:
                token_events = []
            native_eligible = []
            for event in token_events:
                if not self._valid_event(event, now_ms):
                    continue
                if event.chain != "solana" or event.token_address != token_mint:
                    continue
                if event.quote_token_address != WSOL_MINT:
                    continue
                try:
                    token_price_usd = self._decimal(event.price_usd, "token USD price")
                    token_price_sol = self._decimal(event.price_native, "token native price")
                    liquidity = self._decimal(event.liquidity_usd, "pool liquidity")
                    volume = self._decimal(event.volume_h1_usd, "pool hourly volume")
                except ValueError:
                    continue
                sol_usd = token_price_usd / token_price_sol
                native_eligible.append((liquidity, volume, event, sol_usd))
            if native_eligible:
                _, _, event, price = max(
                    native_eligible, key=lambda item: (item[0], item[1], item[2].observed_at_ms)
                )
                return SolUsdObservation(
                    price, event.observed_at_ms, event.source, event.pair_address,
                    "token_sol_pair_ratio",
                )
        raise RuntimeError("no fresh, sufficiently liquid SOL/USD observation or token/SOL conversion")

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
        sol_observation = self._sol_usd(now_ms, mint)
        sol_usd = sol_observation.price_usd
        lamports = int(
            (token_usd / sol_usd * Decimal(1_000_000_000)).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        if lamports <= 0:
            raise RuntimeError("converted token price is below one lamport")
        return SolanaPaperQuote(
            mint=mint,
            market_snapshot_id=int(token["id"]),
            token_pair_address=str(token["pair_address"]),
            price_lamports=lamports,
            token_price_usd=token_usd,
            sol_price_usd=sol_usd,
            token_observed_at_ms=token_at,
            sol_observed_at_ms=sol_observation.observed_at_ms,
            token_source=token_source,
            sol_source=sol_observation.source,
            sol_usd_pair_address=sol_observation.pair_address,
            sol_usd_method=sol_observation.method,
        )
