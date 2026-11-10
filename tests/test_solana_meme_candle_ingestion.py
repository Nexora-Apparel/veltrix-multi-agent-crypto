from unittest.mock import Mock
from veltrix.solana_meme.candle_data import SolanaCandle
from veltrix.solana_meme.candle_store import SolanaCandleStore
from veltrix.solana_meme.candle_ingestion import SolanaCandleIngestion

MINT="So11111111111111111111111111111111111111112"

def test_ingestion_persists_provider_candles_and_is_idempotent(tmp_path):
    candle=SolanaCandle(mint=MINT,pair_address="pool",interval="1m",
        open_time_ms=1_800_000_000_000,open=.001,high=.0012,low=.0009,
        close=.0011,volume=100,source="geckoterminal",
        observed_at_ms=1_800_000_059_000,is_closed=True)
    provider=Mock()
    provider.name="geckoterminal"
    provider.fetch_candles.return_value=[candle]
    store=SolanaCandleStore(str(tmp_path/"candles.db"))
    service=SolanaCandleIngestion(store,provider)
    first=service.refresh(MINT,"pool","1m")
    second=service.refresh(MINT,"pool","1m")
    assert (first.fetched,first.inserted,first.duplicates)==(1,1,0)
    assert (second.fetched,second.inserted,second.duplicates)==(1,0,1)
    assert store.count(MINT)==1
