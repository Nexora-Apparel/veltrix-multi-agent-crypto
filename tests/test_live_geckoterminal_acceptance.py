"""Opt-in live, read-only GeckoTerminal OHLCV acceptance probe.

Set VELTRIX_LIVE_GECKOTERMINAL_TEST=1 and provide a known Solana mint and
GeckoTerminal pool address. This test never writes to a wallet or chain.
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.request import Request, urlopen

import pytest

from veltrix.solana_meme.candidates import canonical_mint
from veltrix.solana_meme.geckoterminal_candles import GeckoTerminalCandleProvider
from veltrix.solana_meme.candle_store import SolanaCandleStore
from veltrix.solana_meme.candle_ingestion import SolanaCandleIngestion
from veltrix.dashboard_server import build_server


pytestmark = pytest.mark.skipif(
    os.getenv("VELTRIX_LIVE_GECKOTERMINAL_TEST") != "1",
    reason="live GeckoTerminal acceptance is opt-in",
)


def test_live_geckoterminal_returns_valid_recent_ohlcv_and_reaches_dashboard(tmp_path):
    mint = canonical_mint(os.environ["VELTRIX_ACCEPTANCE_SOLANA_MINT"])
    pool = os.environ["VELTRIX_ACCEPTANCE_SOLANA_POOL"]
    interval = os.getenv("VELTRIX_ACCEPTANCE_CANDLE_INTERVAL", "1m")
    started = time.monotonic()
    database = tmp_path / "live-chart.sqlite3"
    store = SolanaCandleStore(str(database))
    provider = GeckoTerminalCandleProvider(timeout_seconds=12)
    ingestion = SolanaCandleIngestion(store, provider)
    result = ingestion.refresh(mint, pool, interval, limit=30)
    latency_ms = round((time.monotonic() - started) * 1000)
    candles = store.history(mint, pool, interval, limit=100)

    assert result.fetched > 0, "GeckoTerminal returned no OHLCV observations for this pool"
    assert result.inserted > 0, "live OHLCV was not persisted by ingestion"
    assert result.duplicates == result.fetched - result.inserted
    assert [item["open_time_ms"] for item in candles] == sorted(
        item["open_time_ms"] for item in candles
    ), "persisted candles must be ordered oldest to newest"
    assert len({item["open_time_ms"] for item in candles}) == len(candles), (
        "persisted candle intervals must be unique"
    )
    assert latency_ms < 15_000, f"provider latency too high: {latency_ms}ms"
    now_ms = time.time_ns() // 1_000_000
    for candle in candles:
        assert candle["mint"] == mint
        assert candle["pair_address"] == pool
        assert candle["interval"] == interval
        assert candle["source"] == "geckoterminal"
        assert candle["open"] > 0 and candle["high"] > 0 and candle["low"] > 0 and candle["close"] > 0
        assert candle["volume"] >= 0
        assert candle["low"] <= min(candle["open"], candle["close"])
        assert candle["high"] >= max(candle["open"], candle["close"])
        assert 0 <= now_ms - candle["observed_at_ms"] <= 120_000
        assert candle["open_time_ms"] <= now_ms + 60_000

    # Exercise the authenticated HTTP route consumed by dashboard.js.
    token = "live-chart-acceptance-token-" + ("x" * 32)
    server = build_server(
        database_path=database, host="127.0.0.1", port=0, api_token=token
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        query = (
            f"mint={mint}&pair={pool}&interval={interval}&limit=100"
        )
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/candles?{query}",
            headers={"Authorization": "Bearer " + token},
        )
        with urlopen(request, timeout=5) as response:
            chart_payload = json.loads(response.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert chart_payload["count"] == len(candles)
    assert chart_payload["source"] == "geckoterminal"
    assert chart_payload["freshness"] == "fresh"
    assert chart_payload["observed_at_ms"] == candles[-1]["observed_at_ms"]
    assert chart_payload["synthetic"] is False
    assert chart_payload["candles"][-1]["close"] == candles[-1]["close"]

    print(
        "LIVE_GECKOTERMINAL_ACCEPTANCE "
        f"mint={mint} pool={pool} interval={interval} "
        f"candles={len(candles)} inserted={result.inserted} "
        f"chart_count={chart_payload['count']} latency_ms={latency_ms} "
        f"latest_open_ms={candles[-1]['open_time_ms']}"
    )
