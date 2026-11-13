"""One-shot live DEX Screener market-data check for Phase 04.

Run with:
    python -m veltrix.solana_meme.market_smoke

The check uses the established BONK Solana mint as a stable market-data probe.
It reads one token-pairs response and performs no wallet or transaction actions.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..providers.dexscreener import DexScreenerProvider
from .market_data import MarketDataUnavailable, SolanaMarketData, SolanaMarketSnapshot

BONK_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


@dataclass(frozen=True)
class MarketSmokeResult:
    snapshot: SolanaMarketSnapshot
    pair_count: int


class _OneShotProvider:
    def __init__(self, events):
        self.events = events

    def fetch_token_pairs(self, chain, token_addresses):
        return self.events


def run_market_smoke(provider=None) -> MarketSmokeResult:
    provider = provider or DexScreenerProvider()
    events = provider.fetch_token_pairs("solana", [BONK_MINT])
    if not events:
        diagnostic = "normalized event count=0"
        if hasattr(provider, "session") and hasattr(provider, "base_url"):
            try:
                url = f"{provider.base_url}/tokens/v1/solana/{BONK_MINT}"
                response = provider.session.get(url, timeout=provider.timeout)
                response.raise_for_status()
                raw = response.json()
                if isinstance(raw, list):
                    sample = []
                    for pair in raw[:3]:
                        if not isinstance(pair, dict):
                            continue
                        base = pair.get("baseToken")
                        quote = pair.get("quoteToken")
                        sample.append({
                            "chainId": pair.get("chainId"),
                            "pairAddress": pair.get("pairAddress"),
                            "baseAddress": base.get("address") if isinstance(base, dict) else None,
                            "quoteAddress": quote.get("address") if isinstance(quote, dict) else None,
                            "priceUsd": pair.get("priceUsd"),
                            "priceNative": pair.get("priceNative"),
                        })
                    diagnostic = f"raw pair count={len(raw)}; sample={sample}"
                else:
                    diagnostic = f"unexpected raw response type={type(raw).__name__}"
            except Exception as exc:
                diagnostic = f"raw diagnostic request failed ({type(exc).__name__})"
        raise MarketDataUnavailable(
            f"DEX Screener returned no normalized BONK pair data; {diagnostic}"
        )

    snapshot = SolanaMarketData(_OneShotProvider(events)).fetch(BONK_MINT)
    if snapshot.price_usd <= 0 or snapshot.pair_count < 1:
        raise RuntimeError("DEX Screener returned no usable BONK market snapshot")
    return MarketSmokeResult(snapshot=snapshot, pair_count=snapshot.pair_count)


def main() -> None:
    result = run_market_smoke()
    snapshot = result.snapshot
    print("DEX Screener Solana token-pairs request: PASS")
    print(f"Mint: {snapshot.mint}")
    print(f"Selected DEX/pair: {snapshot.dex_id or 'unknown'} / {snapshot.selected_pair_address}")
    print(f"Pool token side: {snapshot.pool_token_side or 'unspecified'}")
    print(f"USD price: {snapshot.price_usd}")
    print(f"Selected-pool liquidity USD: {snapshot.selected_pool_liquidity_usd}")
    print(f"Eligible pools: {result.pair_count}")
    print(f"Data-quality flags: {', '.join(snapshot.quality_flags) or 'none'}")


if __name__ == "__main__":
    main()
