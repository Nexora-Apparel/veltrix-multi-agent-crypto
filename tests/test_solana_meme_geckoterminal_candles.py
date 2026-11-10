from unittest.mock import Mock

import pytest

from veltrix.solana_meme.geckoterminal_candles import GeckoTerminalCandleProvider

MINT="So11111111111111111111111111111111111111112"
POOL="pool-address"


def response(rows):
    result=Mock()
    result.raise_for_status.return_value=None
    result.json.return_value={"data":{"attributes":{"ohlcv_list":rows}}}
    return result


def test_fetches_and_normalizes_real_provider_ohlcv_rows():
    session=Mock()
    session.get.return_value=response([
        [1800000000,0.001,0.0012,0.0009,0.0011,1200],
        [1800000060,0.0011,0.0013,0.0010,0.0012,1500],
    ])
    provider=GeckoTerminalCandleProvider(session=session,clock_ms=lambda:1_800_000_120_000)
    candles=provider.fetch_candles(MINT,POOL,"1m",limit=100)
    assert len(candles)==2
    assert candles[0].open_time_ms==1_800_000_000_000
    assert candles[0].close==0.0011
    assert candles[0].source=="geckoterminal"
    assert candles[0].is_closed is True
    assert session.get.call_args.kwargs["params"]["aggregate"]==1


def test_rejects_invalid_inputs_before_network_call():
    session=Mock()
    provider=GeckoTerminalCandleProvider(session=session)
    with pytest.raises(ValueError):
        provider.fetch_candles("bad",POOL,"1m")
    with pytest.raises(ValueError):
        provider.fetch_candles(MINT,POOL,"2m")
    assert session.get.call_count==0


def test_skips_malformed_rows_and_deduplicates_timestamps():
    session=Mock()
    session.get.return_value=response([
        [1800000000,1,1.2,.8,1.1,10],
        ["bad",1,1.2,.8,1.1,10],
        [1800000000,1,1.3,.8,1.2,20],
    ])
    provider=GeckoTerminalCandleProvider(session=session,clock_ms=lambda:1_800_000_100_000)
    candles=provider.fetch_candles(MINT,POOL,"1m")
    assert len(candles)==1
    assert candles[0].close==1.2


def test_provider_errors_do_not_return_fake_candles():
    session=Mock()
    session.get.side_effect=TimeoutError("timeout")
    provider=GeckoTerminalCandleProvider(session=session)
    with pytest.raises(RuntimeError,match="request failed"):
        provider.fetch_candles(MINT,POOL,"1m")
