"""One-shot live DEX Screener market-data check for Phase 04.

Run with:
    python -m veltrix.solana_meme.market_smoke

The check uses the established BONK Solana mint as a stable meme-coin market-data probe. It
reads one token-pairs response and performs no wallet or transaction actions.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..providers.dexscreener import DexScreenerProvider
from .market_data import SolanaMarketData, SolanaMarketSnapshot

BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@dataclass(frozen=True)
class MarketSmokeResult:
    snapshot: SolanaMarketSnapshot
    pair_count: int


def run_market_smoke(provider=None) -> MarketSmokeResult:
    market = SolanaMarketData(provider or DexScreenerProvider())
    snapshot = market.fetch(BONK_MINT)
    if snapshot.price_usd <= 0 or snapshot.pair_count < 1:
        raise RuntimeError("DEX Screener returned no usable BONK market snapshot")
    return MarketSmokeResult(snapshot=snapshot, pair_count=snapshot.pair_count)


def main() -> None:
    result = run_market_smoke()
    snapshot = result.snapshot
    print("DEX Screener Solana token-pairs request: PASS")
    print(f"Mint: {snapshot.mint}")
    print(f"Selected DEX/pair: {snapshot.dex_id or 'unknown'} / {snapshot.selected_pair_address}")
    print(f"USD price: {snapshot.price_usd}")
    print(f"Selected-pool liquidity USD: {snapshot.selected_pool_liquidity_usd}")
    print(f"Eligible pools: {result.pair_count}")
    print(f"Data-quality flags: {', '.join(snapshot.quality_flags) or 'none'}")


if __name__ == "__main__":
    main()
