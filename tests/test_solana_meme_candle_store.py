import pytest

from veltrix.solana_meme.candle_data import SolanaCandle
from veltrix.solana_meme.candle_store import SolanaCandleStore

MINT = "So11111111111111111111111111111111111111112"


def candle(open_time=1_800_000_000_000, source="provider-a", close=0.001):
    return SolanaCandle(
        mint=MINT, pair_address="pool-1", interval="1m",
        open_time_ms=open_time, open=0.0009, high=0.0012,
        low=0.0008, close=close, volume=1200,
        source=source, observed_at_ms=open_time + 59000, is_closed=True,
    )


def test_candle_validates_ohlc_and_interval_alignment():
    with pytest.raises(ValueError, match="high"):
        SolanaCandle(
            mint=MINT, pair_address="pool", interval="1m",
            open_time_ms=1_800_000_000_000, open=1, high=0.9,
            low=0.8, close=0.85, volume=1, source="provider",
            observed_at_ms=1_800_000_059_000,
        )
    with pytest.raises(ValueError, match="align"):
        SolanaCandle(
            mint=MINT, pair_address="pool", interval="1m",
            open_time_ms=1_800_000_000_001, open=1, high=1.2,
            low=0.8, close=1.1, volume=1, source="provider",
            observed_at_ms=1_800_000_059_000,
        )


def test_store_deduplicates_by_provider_and_preserves_provider_provenance(tmp_path):
    store = SolanaCandleStore(str(tmp_path / "candles.sqlite3"))
    assert store.record(candle())
    assert not store.record(candle())
    assert store.record(candle(source="provider-b", close=0.0011))
    assert store.count(MINT) == 2
    rows = store.history(MINT, "pool-1", "1m")
    by_source = {row["source"]: row["close"] for row in rows}
    assert by_source == {"provider-a": 0.001, "provider-b": 0.0011}


def test_store_rejects_invalid_query_bounds(tmp_path):
    store = SolanaCandleStore(str(tmp_path / "candles.sqlite3"))
    with pytest.raises(ValueError):
        store.history(MINT, "pool-1", "2m")
    with pytest.raises(ValueError):
        store.history(MINT, "pool-1", "1m", limit=0)
