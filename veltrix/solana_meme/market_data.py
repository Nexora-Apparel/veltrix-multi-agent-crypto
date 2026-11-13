"""Solana-only market intelligence normalization over DEX Screener pair data.

These observations describe market activity. They are not safety approvals,
buy signals, liquidity guarantees, or proof of executable sell depth.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math
import time
from typing import Callable, Protocol, Sequence

from ..providers.base import MarketEvent, ProviderError
from ..providers.dexscreener import DexScreenerProvider
from .candidates import canonical_mint


class MarketDataUnavailable(RuntimeError):
    """Raised when no usable Solana market snapshot can be established."""


class MarketProvider(Protocol):
    def fetch_token_pairs(self, chain: str, token_addresses: Sequence[str]) -> list[MarketEvent]:
        ...


@dataclass(frozen=True)
class SolanaMarketSnapshot:
    mint: str
    selected_pair_address: str
    symbol: str | None
    name: str | None
    price_usd: float
    selected_pool_liquidity_usd: float
    selected_pool_volume_m5_usd: float | None
    selected_pool_volume_h1_usd: float
    selected_pool_volume_h24_usd: float | None
    price_change_m5_pct: float | None
    price_change_h1_pct: float | None
    price_change_h24_pct: float | None
    txns_m5_buys: int | None
    txns_m5_sells: int | None
    txns_h1_buys: int | None
    txns_h1_sells: int | None
    txns_h24_buys: int | None
    txns_h24_sells: int | None
    fdv_usd: float | None
    market_cap_usd: float | None
    dex_id: str | None
    quote_token_symbol: str | None
    pool_token_side: str | None
    pair_count: int
    retrieved_at_ms: int
    response_received_at_ms: int
    pair_created_at_ms: int | None
    quality_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        canonical_mint(self.mint)
        if not isinstance(self.selected_pair_address, str) or not self.selected_pair_address.strip():
            raise ValueError("selected_pair_address is required")
        object.__setattr__(self, "selected_pair_address", self.selected_pair_address.strip())
        for field in ("symbol", "name", "dex_id", "quote_token_symbol"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be a string or None")
        for field in ("retrieved_at_ms", "response_received_at_ms"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field} must be a positive integer")
        if self.pair_created_at_ms is not None and (
            isinstance(self.pair_created_at_ms, bool)
            or not isinstance(self.pair_created_at_ms, int)
            or self.pair_created_at_ms <= 0
        ):
            raise ValueError("pair_created_at_ms must be a positive integer or None")
        if isinstance(self.pair_count, bool) or not isinstance(self.pair_count, int) or self.pair_count < 1:
            raise ValueError("pair_count must be a positive integer")
        for field in (
            "price_usd",
            "selected_pool_liquidity_usd",
            "selected_pool_volume_h1_usd",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field} must be numeric")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{field} must be finite and non-negative")
        if self.price_usd <= 0:
            raise ValueError("price_usd must be positive")
        for field in (
            "selected_pool_volume_m5_usd",
            "selected_pool_volume_h24_usd",
            "fdv_usd",
            "market_cap_usd",
        ):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{field} must be finite and non-negative or None")
        for field in (
            "price_change_m5_pct",
            "price_change_h1_pct",
            "price_change_h24_pct",
        ):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{field} must be finite or None")
        for field in (
            "txns_m5_buys", "txns_m5_sells",
            "txns_h1_buys", "txns_h1_sells",
            "txns_h24_buys", "txns_h24_sells",
        ):
            value = getattr(self, field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field} must be a non-negative integer or None")
        if self.response_received_at_ms > self.retrieved_at_ms:
            raise ValueError("response_received_at_ms cannot be later than retrieved_at_ms")
        if self.pool_token_side not in (None, "base", "quote"):
            raise ValueError("pool_token_side must be base, quote or None")
        if not isinstance(self.quality_flags, tuple) or any(
            not isinstance(flag, str) or not flag.strip() for flag in self.quality_flags
        ):
            raise ValueError("quality_flags must be a tuple of non-empty strings")
        object.__setattr__(
            self, "quality_flags", tuple(sorted({flag.strip() for flag in self.quality_flags}))
        )

    @property
    def pair_age_seconds(self) -> int | None:
        if self.pair_created_at_ms is None:
            return None
        return max(0, (self.retrieved_at_ms - self.pair_created_at_ms) // 1000)


def validate_market_snapshot(snapshot: object) -> SolanaMarketSnapshot:
    """Reconstruct a snapshot at the persistence boundary.

    A frozen dataclass can be forged with object.__new__ or mutated with
    object.__setattr__; type identity alone is not proof of valid market data.
    """
    if type(snapshot) is not SolanaMarketSnapshot:
        raise TypeError("snapshot must be a normalized SolanaMarketSnapshot")
    try:
        values = {field.name: getattr(snapshot, field.name) for field in fields(SolanaMarketSnapshot)}
        return SolanaMarketSnapshot(**values)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("market snapshot failed canonical validation") from None


@dataclass(frozen=True)
class MarketDataBatch:
    snapshots: dict[str, SolanaMarketSnapshot]
    unavailable: dict[str, str]


class SolanaMarketData:
    """Fetch batched pair data for validated Solana mints and select deepest pools."""

    def __init__(
        self,
        provider: MarketProvider | None = None,
        *,
        max_response_age_ms: int = 120_000,
        minimum_liquidity_usd: float = 0.0,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            isinstance(max_response_age_ms, bool)
            or not isinstance(max_response_age_ms, int)
            or max_response_age_ms <= 0
        ):
            raise ValueError("max_response_age_ms must be a positive integer")
        if (
            isinstance(minimum_liquidity_usd, bool)
            or not isinstance(minimum_liquidity_usd, (int, float))
            or not math.isfinite(minimum_liquidity_usd)
            or minimum_liquidity_usd < 0
        ):
            raise ValueError("minimum_liquidity_usd must be finite and non-negative")
        self.provider = provider or DexScreenerProvider()
        self.max_response_age_ms = max_response_age_ms
        self.minimum_liquidity_usd = float(minimum_liquidity_usd)
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    @staticmethod
    def _nonnegative(value: object, field: str, *, optional: bool = False) -> float | None:
        if value is None and optional:
            return None
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
    def _signed(value: object, field: str, *, optional: bool = False) -> float | None:
        if value is None and optional:
            return None
        if isinstance(value, bool):
            raise MarketDataUnavailable(f"invalid {field}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise MarketDataUnavailable(f"invalid {field}") from exc
        if not math.isfinite(number):
            raise MarketDataUnavailable(f"invalid {field}")
        return number

    @staticmethod
    def _count(value: object, field: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MarketDataUnavailable(f"invalid {field}")
        return value

    def _snapshot(
        self,
        mint: str,
        events: list[MarketEvent],
        *,
        retrieved_at_ms: int,
    ) -> SolanaMarketSnapshot:
        eligible: list[tuple[MarketEvent, dict[str, object]]] = []
        for event in events:
            if not isinstance(event, MarketEvent):
                continue
            if (
                not isinstance(event.quality_flags, tuple)
                or any(not isinstance(flag, str) or not flag.strip() for flag in event.quality_flags)
            ):
                continue
            if event.chain != "solana" or event.token_address != mint:
                continue
            if not isinstance(event.pair_address, str) or not event.pair_address.strip():
                continue
            if (
                isinstance(event.observed_at_ms, bool)
                or not isinstance(event.observed_at_ms, int)
                or event.observed_at_ms <= 0
            ):
                continue
            response_age = retrieved_at_ms - event.observed_at_ms
            if response_age < 0 or response_age > self.max_response_age_ms:
                continue
            if event.pair_created_at_ms is not None and (
                isinstance(event.pair_created_at_ms, bool)
                or not isinstance(event.pair_created_at_ms, int)
                or event.pair_created_at_ms <= 0
            ):
                continue
            try:
                values: dict[str, object] = {
                    "price": self._nonnegative(event.price_usd, "price_usd"),
                    "liquidity": self._nonnegative(event.liquidity_usd, "liquidity_usd"),
                    "volume_h1": self._nonnegative(event.volume_h1_usd, "volume_h1_usd"),
                    "volume_m5": self._nonnegative(
                        event.volume_m5_usd, "volume_m5_usd", optional=True
                    ),
                    "volume_h24": self._nonnegative(
                        event.volume_h24_usd, "volume_h24_usd", optional=True
                    ),
                    "change_m5": self._signed(
                        event.price_change_m5_pct, "price_change_m5_pct", optional=True
                    ),
                    "change_h1": self._signed(
                        event.price_change_h1_pct, "price_change_h1_pct", optional=True
                    ),
                    "change_h24": self._signed(
                        event.price_change_h24_pct, "price_change_h24_pct", optional=True
                    ),
                    "fdv": self._nonnegative(event.fdv_usd, "fdv_usd", optional=True),
                    "market_cap": self._nonnegative(
                        event.market_cap_usd, "market_cap_usd", optional=True
                    ),
                }
                for field in (
                    "txns_m5_buys", "txns_m5_sells",
                    "txns_h1_buys", "txns_h1_sells",
                    "txns_h24_buys", "txns_h24_sells",
                ):
                    values[field] = self._count(getattr(event, field), field)
            except MarketDataUnavailable:
                continue
            if values["price"] is None or values["liquidity"] is None or values["volume_h1"] is None:
                continue
            if values["price"] <= 0:
                continue
            eligible.append((event, values))

        if not eligible:
            raise MarketDataUnavailable(f"no fresh usable Solana pair data for {mint}")

        selected, values = max(
            eligible,
            key=lambda item: (
                "liquidity_usd_missing" not in item[0].quality_flags,
                float(item[1]["liquidity"]),
                float(item[1]["volume_h1"]),
                item[0].pair_address,
            ),
        )
        flags: list[str] = list(selected.quality_flags)
        if (
            "liquidity_usd_missing" not in flags
            and float(values["liquidity"]) < self.minimum_liquidity_usd
        ):
            flags.append("below_configured_minimum_liquidity")
        if float(values["liquidity"]) == 0 and "liquidity_usd_missing" not in flags:
            flags.append("zero_liquidity")
        if float(values["volume_h1"]) == 0 and "volume_h1_usd_missing" not in flags:
            flags.append("zero_hourly_volume")
        if values["volume_m5"] is None:
            flags.append("volume_m5_missing")
        if values["volume_h24"] is None:
            flags.append("volume_h24_missing")
        if values["change_m5"] is None:
            flags.append("price_change_m5_missing")
        if values["change_h1"] is None:
            flags.append("price_change_h1_missing")
        if values["change_h24"] is None:
            flags.append("price_change_h24_missing")
        if values["market_cap"] is None:
            flags.append("market_cap_missing")
        if values["fdv"] is None:
            flags.append("fdv_missing")
        if selected.pair_created_at_ms is None:
            flags.append("pair_creation_time_missing")
        elif selected.pair_created_at_ms > retrieved_at_ms:
            flags.append("pair_creation_time_in_future")
        if values["txns_h1_buys"] is None or values["txns_h1_sells"] is None:
            flags.append("hourly_transaction_counts_missing")

        return SolanaMarketSnapshot(
            mint=mint,
            selected_pair_address=selected.pair_address,
            symbol=selected.symbol,
            name=selected.name,
            price_usd=float(values["price"]),
            selected_pool_liquidity_usd=float(values["liquidity"]),
            selected_pool_volume_m5_usd=values["volume_m5"],
            selected_pool_volume_h1_usd=float(values["volume_h1"]),
            selected_pool_volume_h24_usd=values["volume_h24"],
            price_change_m5_pct=values["change_m5"],
            price_change_h1_pct=values["change_h1"],
            price_change_h24_pct=values["change_h24"],
            txns_m5_buys=values["txns_m5_buys"],
            txns_m5_sells=values["txns_m5_sells"],
            txns_h1_buys=values["txns_h1_buys"],
            txns_h1_sells=values["txns_h1_sells"],
            txns_h24_buys=values["txns_h24_buys"],
            txns_h24_sells=values["txns_h24_sells"],
            fdv_usd=values["fdv"],
            market_cap_usd=values["market_cap"],
            dex_id=selected.dex_id,
            quote_token_symbol=selected.quote_token_symbol,
            pool_token_side=selected.pool_token_side,
            pair_count=len(eligible),
            retrieved_at_ms=retrieved_at_ms,
            response_received_at_ms=selected.observed_at_ms,
            pair_created_at_ms=selected.pair_created_at_ms,
            quality_flags=tuple(sorted(flags)),
        )

    def fetch_many(
        self,
        mints: Sequence[str],
        *,
        now_ms: int | None = None,
    ) -> MarketDataBatch:
        if isinstance(mints, str) or not isinstance(mints, Sequence):
            raise ValueError("mints must be a sequence of Solana addresses")
        canonical_mints = list(dict.fromkeys(canonical_mint(mint) for mint in mints))
        if not canonical_mints:
            return MarketDataBatch({}, {})
        retrieved = now_ms
        if retrieved is not None and (
            isinstance(retrieved, bool) or not isinstance(retrieved, int) or retrieved <= 0
        ):
            raise ValueError("now_ms must be a positive integer")
        try:
            events = self.provider.fetch_token_pairs("solana", canonical_mints)
        except ProviderError as exc:
            raise MarketDataUnavailable(str(exc)) from exc
        if not isinstance(events, (list, tuple)):
            raise MarketDataUnavailable("market provider returned an invalid event collection")
        # Provider response timestamps are captured after the HTTP response.
        # Capture the enclosing retrieval timestamp after fetch as well so the
        # response cannot appear spuriously future-dated by request latency.
        if retrieved is None:
            retrieved = self.clock_ms()
        if isinstance(retrieved, bool) or not isinstance(retrieved, int) or retrieved <= 0:
            raise ValueError("market clock must return a positive integer timestamp")

        grouped: dict[str, list[MarketEvent]] = {mint: [] for mint in canonical_mints}
        for event in events:
            if isinstance(event, MarketEvent) and event.token_address in grouped:
                grouped[event.token_address].append(event)

        snapshots: dict[str, SolanaMarketSnapshot] = {}
        unavailable: dict[str, str] = {}
        for mint in canonical_mints:
            try:
                snapshots[mint] = self._snapshot(
                    mint, grouped[mint], retrieved_at_ms=retrieved
                )
            except MarketDataUnavailable as exc:
                unavailable[mint] = str(exc)
        return MarketDataBatch(snapshots, unavailable)

    def fetch(self, mint: str, *, now_ms: int | None = None) -> SolanaMarketSnapshot:
        canonical = canonical_mint(mint)
        batch = self.fetch_many([canonical], now_ms=now_ms)
        snapshot = batch.snapshots.get(canonical)
        if snapshot is None:
            raise MarketDataUnavailable(
                batch.unavailable.get(canonical, f"no market snapshot for {canonical}")
            )
        return snapshot
