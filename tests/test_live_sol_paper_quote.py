"""Opt-in live SOL/USD quote acceptance; read-only public market data."""
import os
import time
from decimal import Decimal

import pytest

from veltrix.providers.dexscreener import DexScreenerProvider
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore
from veltrix.solana_meme.sol_paper_quote import SolanaPaperQuoteAdapter
from veltrix.solana_meme.sol_paper_wallet import LAMPORTS_PER_SOL, SolanaPaperWallet

BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@pytest.mark.skipif(
    os.getenv("VELTRIX_LIVE_SOL_QUOTE_TEST") != "1",
    reason="set VELTRIX_LIVE_SOL_QUOTE_TEST=1 for public SOL/USD acceptance",
)
def test_live_dexscreener_sol_quote_converts_real_bonk_market_to_lamports(tmp_path):
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
    assert quote.sol_usd_method in {"wsol_stable_pool", "token_sol_pair_ratio"}
    print(
        "LIVE_SOL_PAPER_QUOTE_ACCEPTANCE "
        f"token_price_usd={quote.token_price_usd} "
        f"sol_price_usd={quote.sol_price_usd} "
        f"price_lamports={quote.price_lamports} "
        f"sol_pool={quote.sol_usd_pair_address} "
        f"conversion_method={quote.sol_usd_method} "
        f"token_observed_at_ms={quote.token_observed_at_ms} "
        f"sol_observed_at_ms={quote.sol_observed_at_ms}"
    )

    # Execute a bounded, entirely simulated SOL-denominated round trip using
    # the just-observed provider quote. No signer or transaction RPC exists.
    provenance = {
        "mint": quote.mint,
        "market_snapshot_id": quote.market_snapshot_id,
        "token_pair_address": quote.token_pair_address,
        "sol_usd_pair_address": quote.sol_usd_pair_address,
        "sol_usd_method": quote.sol_usd_method,
        "token_price_usd": str(quote.token_price_usd),
        "sol_price_usd": str(quote.sol_price_usd),
        "token_observed_at_ms": quote.token_observed_at_ms,
        "sol_observed_at_ms": quote.sol_observed_at_ms,
        "token_source": quote.token_source,
        "sol_source": quote.sol_source,
    }
    wallet_path = str(tmp_path / "live-sol-paper-wallet.sqlite3")
    wallet = SolanaPaperWallet(wallet_path, LAMPORTS_PER_SOL)
    quantity = Decimal("5") / quote.token_price_usd
    entry_notional = int(quantity * quote.price_lamports)
    entry_fee = entry_notional // 100
    wallet.open_position(
        idempotency_key="live-provider-paper-entry",
        mint=quote.mint, quantity=quantity,
        price_lamports=quote.price_lamports, fee_lamports=entry_fee,
        created_at_ms=now_ms, quote_provenance=provenance,
    )
    wallet.close_position(
        idempotency_key="live-provider-paper-exit",
        mint=quote.mint, price_lamports=quote.price_lamports,
        fee_lamports=entry_notional // 100,
        created_at_ms=now_ms + 1, quote_provenance=provenance,
    )
    recovered = SolanaPaperWallet(wallet_path, 99 * LAMPORTS_PER_SOL).snapshot()
    assert recovered["open_positions"] == []
    assert len(recovered["fills"]) == 2
    assert [fill["side"] for fill in reversed(recovered["fills"])] == ["BUY", "SELL"]
    assert all(fill["quote_provenance"]["sol_usd_pair_address"] == quote.sol_usd_pair_address
               for fill in recovered["fills"])
    assert recovered["cash_lamports"] < LAMPORTS_PER_SOL
    assert recovered["realized_pnl_lamports"] < 0
    print(
        "LIVE_SOL_PAPER_LEDGER_ACCEPTANCE "
        f"starting_balance_lamports={recovered['starting_balance_lamports']} "
        f"cash_lamports={recovered['cash_lamports']} "
        f"realized_pnl_lamports={recovered['realized_pnl_lamports']} "
        f"fills={len(recovered['fills'])} recovered=true"
    )
