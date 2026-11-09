"""One-shot live DEX Screener market-data check for Phase 04.

Run with:
    python -m veltrix.solana_meme.market_smoke

The check uses the public wrapped-SOL mint as a stable market-data probe. It
reads one token-pairs response and performs no wallet or transaction actions.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..providers.dexscreener import DexScreenerProvider
from .market_data import SolanaMarketData, SolanaMarketSnapshot

WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True)
class MarketSmokeResult:
    snapshot: SolanaMarketSnapshot
    pair_count: int


def run_market_smoke(provider=None) -> MarketSmokeResult:
    market = SolanaMarketData(provider or DexScreenerProvider())
    snapshot = market.fetch(WRAPPED_SOL_MINT)
    if snapshot.price_usd <= 0 or snapshot.pair_count < 1:
        raise RuntimeError("DEX Screener returned no usable wrapped-SOL market snapshot")
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
