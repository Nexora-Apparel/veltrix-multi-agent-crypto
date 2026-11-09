"""Validated read-only DEX Screener token-pair provider."""
from __future__ import annotations

import math
import time
from collections.abc import Sequence
from typing import Any

import requests

from .base import MarketEvent, ProviderError

CHAIN_IDS = {"solana": "solana", "base": "base", "bsc": "bsc"}


class DexScreenerProvider:
    """Read-only market snapshots; pair data is not a token-safety audit."""

    name = "dexscreener"
    batch_size = 30

    def __init__(
        self,
        base_url: str = "https://api.dexscreener.com",
        timeout: float = 10.0,
        session: Any | None = None,
        *,
        clock_ms: Any | None = None,
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a finite positive number")
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.session = session or requests.Session()
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def fetch_token_pairs(self, chain: str, token_addresses: Sequence[str]) -> list[MarketEvent]:
        chain_id = CHAIN_IDS.get(str(chain).lower())
        if chain_id is None:
            raise ProviderError(f"Unsupported chain: {chain}")
        if isinstance(token_addresses, str) or not isinstance(token_addresses, Sequence):
            raise ProviderError("token_addresses must be a sequence of addresses")

        addresses: list[str] = []
        seen: set[str] = set()
        for address in token_addresses:
            if not isinstance(address, str) or not address or address != address.strip():
                raise ProviderError("token address must be a non-empty trimmed string")
            if address not in seen:
                addresses.append(address)
                seen.add(address)
        if not addresses:
            return []

        events: list[MarketEvent] = []
        for offset in range(0, len(addresses), self.batch_size):
            batch = addresses[offset:offset + self.batch_size]
            url = f"{self.base_url}/tokens/v1/{chain_id}/" + ",".join(batch)
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise ProviderError(f"DexScreener request failed: {exc}") from exc
            except Exception as exc:
                raise ProviderError(
                    f"DexScreener request failed: {type(exc).__name__}: {exc}"
                ) from exc
            if not isinstance(payload, list):
                raise ProviderError("Unexpected DexScreener response shape")

            observed_at_ms = self.clock_ms()
            if (
                isinstance(observed_at_ms, bool)
                or not isinstance(observed_at_ms, int)
                or observed_at_ms <= 0
            ):
                raise ProviderError("Invalid local provider response timestamp")
            for pair in payload:
                for requested_address in batch:
                    try:
                        event = self._normalize(
                            pair, chain_id, observed_at_ms, requested_address
                        )
                    except ProviderError:
                        continue
                    if event is not None:
                        events.append(event)
        return events

    @staticmethod
    def _number(value: Any, field: str, *, optional: bool = False) -> float | None:
        if value is None and optional:
            return None
        if isinstance(value, bool):
            raise ProviderError(f"Invalid numeric field: {field}")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"Invalid numeric field: {field}") from exc
        if not math.isfinite(result) or result < 0:
            raise ProviderError(f"Out-of-range numeric field: {field}")
        return result

    @staticmethod
    def _signed_number(
        value: Any,
        field: str,
        *,
        default: float = 0.0,
        optional: bool = False,
    ) -> float | None:
        if value is None:
            return None if optional else default
        if isinstance(value, bool):
            raise ProviderError(f"Invalid numeric field: {field}")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"Invalid numeric field: {field}") from exc
        if not math.isfinite(result):
            raise ProviderError(f"Out-of-range numeric field: {field}")
        return result

    @staticmethod
    def _count(value: Any, field: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ProviderError(f"Invalid transaction count: {field}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"Invalid transaction count: {field}") from exc
        if not math.isfinite(number) or number < 0 or not number.is_integer():
            raise ProviderError(f"Out-of-range transaction count: {field}")
        return int(number)

    @classmethod
    def _normalize(
        cls,
        pair: Any,
        chain: str,
        observed_at_ms: int,
        token_address: str | None = None,
    ) -> MarketEvent | None:
        if not isinstance(pair, dict) or pair.get("chainId") != chain:
            return None
        base = pair.get("baseToken")
        quote = pair.get("quoteToken")
        if not isinstance(base, dict) or not isinstance(quote, dict):
            return None
        base_address = base.get("address")
        quote_address = quote.get("address")
        pair_address = pair.get("pairAddress")
        if not isinstance(base_address, str) or not base_address.strip():
            return None
        if not isinstance(quote_address, str) or not quote_address.strip():
            return None
        if not isinstance(pair_address, str) or not pair_address.strip():
            return None

        requested = token_address or base_address
        if requested == base_address:
            side = "base"
            token = base
            other = quote
            price_usd = cls._number(pair.get("priceUsd"), "priceUsd") or 0.0
            fdv = cls._number(pair.get("fdv"), "fdv", optional=True)
            market_cap = cls._number(pair.get("marketCap"), "marketCap", optional=True)
            price_change = pair.get("priceChange")
            price_change = price_change if isinstance(price_change, dict) else {}
            change_m5 = cls._signed_number(
                price_change.get("m5"), "priceChange.m5", optional=True
            )
            change_h1 = cls._signed_number(
                price_change.get("h1"), "priceChange.h1", optional=True
            ) if price_change.get("h1") is not None else None
            change_h24 = cls._signed_number(
                price_change.get("h24"), "priceChange.h24", optional=True
            ) if price_change.get("h24") is not None else None
        elif requested == quote_address:
            side = "quote"
            token = quote
            other = base
            base_price = cls._number(pair.get("priceUsd"), "priceUsd") or 0.0
            price_native = cls._number(pair.get("priceNative"), "priceNative")
            if price_native is None or price_native <= 0:
                raise ProviderError("Invalid priceNative for quote-token valuation")
            price_usd = base_price / price_native
            # FDV, market cap and price changes in this payload describe the base token.
            fdv = None
            market_cap = None
            change_m5 = change_h1 = change_h24 = None
        else:
            return None

        liquidity = pair.get("liquidity")
        volume = pair.get("volume")
        transactions = pair.get("txns")
        liquidity = liquidity if isinstance(liquidity, dict) else {}
        volume = volume if isinstance(volume, dict) else {}
        transactions = transactions if isinstance(transactions, dict) else {}

        def tx(period: str, direction: str) -> int | None:
            period_data = transactions.get(period)
            if not isinstance(period_data, dict):
                return None
            return cls._count(period_data.get(direction), f"txns.{period}.{direction}")

        created = pair.get("pairCreatedAt")
        if created is not None:
            if isinstance(created, bool):
                raise ProviderError("Invalid pairCreatedAt")
            try:
                created_number = float(created)
            except (TypeError, ValueError) as exc:
                raise ProviderError("Invalid pairCreatedAt") from exc
            if (
                not math.isfinite(created_number)
                or created_number <= 0
                or not created_number.is_integer()
            ):
                raise ProviderError("Invalid pairCreatedAt")
            created = int(created_number)

        return MarketEvent(
            chain=chain,
            token_address=requested,
            pair_address=pair_address.strip(),
            symbol=token.get("symbol") if isinstance(token.get("symbol"), str) else None,
            price_usd=price_usd,
            liquidity_usd=cls._number(liquidity.get("usd", 0), "liquidity.usd") or 0.0,
            volume_h1_usd=cls._number(volume.get("h1", 0), "volume.h1") or 0.0,
            price_change_m5_pct=change_m5,
            observed_at_ms=observed_at_ms,
            pair_created_at_ms=created,
            source=cls.name,
            name=token.get("name") if isinstance(token.get("name"), str) else None,
            fdv_usd=fdv,
            market_cap_usd=market_cap,
            volume_m5_usd=cls._number(volume.get("m5"), "volume.m5", optional=True),
            volume_h24_usd=cls._number(volume.get("h24"), "volume.h24", optional=True),
            price_change_h1_pct=change_h1,
            price_change_h24_pct=change_h24,
            txns_m5_buys=tx("m5", "buys"),
            txns_m5_sells=tx("m5", "sells"),
            txns_h1_buys=tx("h1", "buys"),
            txns_h1_sells=tx("h1", "sells"),
            txns_h24_buys=tx("h24", "buys"),
            txns_h24_sells=tx("h24", "sells"),
            dex_id=pair.get("dexId") if isinstance(pair.get("dexId"), str) else None,
            quote_token_symbol=other.get("symbol") if isinstance(other.get("symbol"), str)
            else None,
            pool_token_side=side,
        )
