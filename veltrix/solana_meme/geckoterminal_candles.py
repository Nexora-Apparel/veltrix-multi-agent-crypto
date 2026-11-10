"""Read-only GeckoTerminal OHLCV provider for Solana pools."""
from __future__ import annotations

import math
import time
from typing import Any, Callable

import requests

from .candle_data import INTERVAL_SECONDS, SolanaCandle


class GeckoTerminalCandleProvider:
    """Fetch historical OHLCV from GeckoTerminal's public pool endpoint.

    The provider returns raw observations only. It does not interpolate missing
    candles, infer prices, or turn ticker snapshots into historical bars.
    """

    name = "geckoterminal"
    base_url = "https://api.geckoterminal.com/api/v2"

    _TIMEFRAMES = {
        "1m": ("minute", 1),
        "5m": ("minute", 5),
        "15m": ("minute", 15),
        "1h": ("hour", 1),
        "4h": ("hour", 4),
        "1d": ("day", 1),
    }

    def __init__(
        self, *, session: Any | None = None, timeout_seconds: float = 10.0,
        base_url: str | None = None, clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int,float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.base_url = (base_url or self.base_url).rstrip("/")
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)

    def fetch_candles(self, mint: str, pair_address: str, interval: str, *, limit: int = 500) -> list[SolanaCandle]:
        from .candidates import canonical_mint

        canonical_mint(mint)
        if not isinstance(pair_address,str) or not pair_address.strip() or len(pair_address)>128:
            raise ValueError("pair_address is required")
        if interval not in INTERVAL_SECONDS or interval not in self._TIMEFRAMES:
            raise ValueError("unsupported candle interval")
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=1000:
            raise ValueError("limit must be between 1 and 1000")
        timeframe, aggregate = self._TIMEFRAMES[interval]
        url=f"{self.base_url}/networks/solana/pools/{pair_address}/ohlcv/{timeframe}"
        try:
            response=self.session.get(
                url, params={"aggregate":aggregate,"limit":limit,"currency":"usd"},
                headers={"accept":"application/json"}, timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload=response.json()
        except Exception as exc:
            raise RuntimeError(f"GeckoTerminal OHLCV request failed: {type(exc).__name__}") from exc
        try:
            rows=payload["data"]["attributes"]["ohlcv_list"]
        except (TypeError,KeyError) as exc:
            raise RuntimeError("GeckoTerminal returned an unexpected OHLCV payload") from exc
        if not isinstance(rows,list):
            raise RuntimeError("GeckoTerminal OHLCV list is invalid")
        observed=self.clock_ms()
        candles=[]
        for row in rows:
            if not isinstance(row,list) or len(row)<6:
                continue
            try:
                timestamp,open_,high,low,close,volume=row[:6]
                if isinstance(timestamp,bool) or not isinstance(timestamp,(int,float)) or not math.isfinite(timestamp):
                    continue
                open_ms=int(timestamp)*1000
                candle=SolanaCandle(
                    mint=mint,pair_address=pair_address,interval=interval,
                    open_time_ms=open_ms,open=float(open_),high=float(high),
                    low=float(low),close=float(close),volume=float(volume),
                    source=self.name,observed_at_ms=observed,
                    is_closed=(observed >= open_ms + INTERVAL_SECONDS[interval]*1000),
                )
            except (TypeError,ValueError,OverflowError):
                continue
            candles.append(candle)
        candles.sort(key=lambda item:item.open_time_ms)
        dedup={item.open_time_ms:item for item in candles}
        return list(dedup.values())
