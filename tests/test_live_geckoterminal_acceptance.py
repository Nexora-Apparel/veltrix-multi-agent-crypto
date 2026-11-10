"""Opt-in live, read-only GeckoTerminal OHLCV acceptance probe.

Set VELTRIX_LIVE_GECKOTERMINAL_TEST=1 and provide a known Solana mint and
GeckoTerminal pool address. This test never writes to a wallet or chain.
"""
from __future__ import annotations

import os
import time

import pytest

from veltrix.solana_meme.candidates import canonical_mint
from veltrix.solana_meme.geckoterminal_candles import GeckoTerminalCandleProvider


pytestmark = pytest.mark.skipif(
    os.getenv("VELTRIX_LIVE_GECKOTERMINAL_TEST") != "1",
    reason="live GeckoTerminal acceptance is opt-in",
)


def test_live_geckoterminal_returns_valid_recent_ohlcv():
    mint = canonical_mint(os.environ["VELTRIX_ACCEPTANCE_SOLANA_MINT"])
    pool = os.environ["VELTRIX_ACCEPTANCE_SOLANA_POOL"]
    interval = os.getenv("VELTRIX_ACCEPTANCE_CANDLE_INTERVAL", "1m")
    started = time.monotonic()
    candles = GeckoTerminalCandleProvider(timeout_seconds=12).fetch_candles(
        mint, pool, interval, limit=30
    )
    latency_ms = round((time.monotonic() - started) * 1000)

    assert candles, "GeckoTerminal returned no OHLCV observations for this pool"
    assert latency_ms < 15_000, f"provider latency too high: {latency_ms}ms"
    now_ms = time.time_ns() // 1_000_000
    for candle in candles:
        assert candle.mint == mint
        assert candle.pair_address == pool
        assert candle.interval == interval
        assert candle.source == "geckoterminal"
        assert candle.open > 0 and candle.high > 0 and candle.low > 0 and candle.close > 0
        assert candle.volume >= 0
        assert candle.low <= min(candle.open, candle.close)
        assert candle.high >= max(candle.open, candle.close)
        assert 0 <= now_ms - candle.observed_at_ms <= 120_000
        assert candle.open_time_ms <= now_ms + 60_000

    print(
        "LIVE_GECKOTERMINAL_ACCEPTANCE "
        f"mint={mint} pool={pool} interval={interval} "
        f"candles={len(candles)} latency_ms={latency_ms} "
        f"latest_open_ms={candles[-1].open_time_ms}"
    )
