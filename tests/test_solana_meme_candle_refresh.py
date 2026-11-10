from veltrix.solana_meme.candle_data import SolanaCandle
from veltrix.solana_meme.candle_refresh import SolanaCandleRefreshWorker
from veltrix.solana_meme.candle_store import SolanaCandleStore
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.market_data import SolanaMarketData

MINT="So11111111111111111111111111111111111111112"


class CandleProvider:
    name="fixture"
    def __init__(self, clock):
        self.clock=clock
        self.calls=[]
    def fetch_candles(self,mint,pair,interval,*,limit):
        self.calls.append((mint,pair,interval,limit))
        now=self.clock()
        return [SolanaCandle(
            mint=mint,pair_address=pair,interval=interval,open_time_ms=now-now%60000,
            open=.001,high=.0012,low=.0009,close=.0011,volume=100,
            source="fixture",observed_at_ms=now,is_closed=False,
        )]


def seed_market(path, now):
    store=MarketSnapshotStore(path)
    event=MarketEvent(
        chain="solana",token_address=MINT,pair_address="pool-a",symbol="M",
        price_usd=.001,liquidity_usd=5000,volume_h1_usd=100,
        price_change_m5_pct=1,observed_at_ms=now,pair_created_at_ms=now-1000,
        source="dexscreener",
    )
    snapshot=SolanaMarketData(
        type("P",(),{"fetch_token_pairs":lambda self,chain,addresses:[event]})(),
        clock_ms=lambda:now,
    ).fetch(MINT,now_ms=now)
    store.record(snapshot)


def test_refresh_discovers_pool_fetches_and_persists_idempotently(tmp_path):
    path=str(tmp_path/"candle-worker.sqlite3")
    now=1_800_000_000_000
    seed_market(path,now)
    clock=lambda:now
    store=SolanaCandleStore(path)
    provider=CandleProvider(clock)
    worker=SolanaCandleRefreshWorker(path,store,provider,clock_ms=clock)
    result=worker.run_once()
    assert result.processed==1
    assert result.fetched==1
    assert result.inserted==1
    assert result.failed==0
    assert store.count(MINT)==1
    assert provider.calls==[(MINT,"pool-a","1m",180)]


def test_refresh_does_not_create_jobs_without_verified_market_pool(tmp_path):
    path=str(tmp_path/"empty.sqlite3")
    MarketSnapshotStore(path)
    store=SolanaCandleStore(path)
    worker=SolanaCandleRefreshWorker(path,store,CandleProvider(lambda:1_800_000_000_000),
                                     clock_ms=lambda:1_800_000_000_000)
    result=worker.run_once()
    assert result.processed==0
    assert result.discovered==0
    assert store.count()==0
