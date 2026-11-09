import time

import pytest

from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.market_data import MarketDataUnavailable
from veltrix.solana_meme.market_smoke import WRAPPED_SOL_MINT, run_market_smoke


class FakeProvider:
    def __init__(self, events):
        self.events = events

    def fetch_token_pairs(self, chain, token_addresses):
        assert chain == "solana"
        assert token_addresses == [WRAPPED_SOL_MINT]
        return self.events


def test_live_market_smoke_normalizes_wrapped_sol_snapshot():
    now = int(time.time() * 1000)
    event = MarketEvent(
        chain="solana", token_address=WRAPPED_SOL_MINT, pair_address="pool",
        symbol="SOL", name="Wrapped SOL", price_usd=140,
        liquidity_usd=1_000_000, volume_h1_usd=100_000,
        price_change_m5_pct=0.2, observed_at_ms=now,
        pair_created_at_ms=now - 1_000, source="dexscreener",
    )
    result = run_market_smoke(FakeProvider([event]))
    assert result.snapshot.mint == WRAPPED_SOL_MINT
    assert result.snapshot.price_usd == 140
    assert result.pair_count == 1


def test_market_smoke_fails_when_provider_has_no_usable_pairs():
    with pytest.raises(MarketDataUnavailable):
        run_market_smoke(FakeProvider([]))
