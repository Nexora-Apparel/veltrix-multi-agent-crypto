"""Read-only token spot-price resolver backed by DexScreener snapshots."""
from .base import ProviderError
from .dexscreener import DexScreenerProvider


class DexScreenerPriceResolver:
    def __init__(self, provider=None, chain="solana", min_liquidity_usd=0.0):
        self.provider = provider or DexScreenerProvider()
        self.chain = chain
        self.min_liquidity_usd = float(min_liquidity_usd)
        if self.min_liquidity_usd < 0:
            raise ValueError("Minimum liquidity cannot be negative")

    def __call__(self, token_address):
        pairs = self.provider.fetch_token_pairs(self.chain, [token_address])
        eligible = [
            pair for pair in pairs
            if pair.token_address == token_address
            and pair.price_usd > 0
            and pair.liquidity_usd >= self.min_liquidity_usd
        ]
        if not eligible:
            raise ProviderError(f"No eligible DexScreener spot price for {token_address}")
        # Prefer the deepest available pool to reduce thin-pool price distortion.
        selected = max(eligible, key=lambda pair: pair.liquidity_usd)
        return selected.price_usd
