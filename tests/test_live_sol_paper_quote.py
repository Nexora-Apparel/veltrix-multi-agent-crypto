"""Opt-in live SOL/USD quote acceptance; read-only public market data."""
import os
import time

import pytest

from veltrix.providers.dexscreener import DexScreenerProvider
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.sol_paper_quote import SolanaPaperQuoteAdapter

WSOL_MINT = "So11111111111111111111111111111111111111112"


@pytest.mark.skipif(
    os.getenv("VELTRIX_LIVE_SOL_QUOTE_TEST") != "1",
    reason="set VELTRIX_LIVE_SOL_QUOTE_TEST=1 for public SOL/USD acceptance",
)
def test_live_dexscreener_sol_quote_converts_real_usdc_market_to_lamports(tmp_path):
    now_ms = int(time.time() * 1000)
    provider = DexScreenerProvider()
    market_data = SolanaMarketData(provider, clock_ms=lambda: int(time.time() * 1000))
    token_snapshot = market_data.fetch(BONK_MINT)
    database = str(tmp_path / "live-sol-quote.sqlite3")
    store = MarketSnapshotStore(database)
    assert store.record(token_snapshot)

    adapter = SolanaPaperQuoteAdapter(
        store, provider, max_age_ms=120_000,
        min_sol_pool_liquidity_usd=50_000,
        min_sol_pool_volume_h1_usd=10_000,
        clock_ms=lambda: int(time.time() * 1000),
    )
    quote = adapter.quote(BONK_MINT)
    assert quote.price_lamports > 0
    assert quote.token_source == "dexscreener"
    assert quote.sol_source == "dexscreener"
    assert quote.token_observed_at_ms <= int(time.time() * 1000)
    assert quote.sol_observed_at_ms <= int(time.time() * 1000)
    assert quote.sol_usd_pair_address
    print(
        "LIVE_SOL_PAPER_QUOTE_ACCEPTANCE "
        f"token_price_usd={quote.token_price_usd} "
        f"sol_price_usd={quote.sol_price_usd} "
        f"price_lamports={quote.price_lamports} "
        f"sol_pool={quote.sol_usd_pair_address} "
        f"token_observed_at_ms={quote.token_observed_at_ms} "
        f"sol_observed_at_ms={quote.sol_observed_at_ms}"
    )
