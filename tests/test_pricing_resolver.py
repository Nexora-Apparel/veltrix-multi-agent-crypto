from types import SimpleNamespace
from veltrix.providers.pricing import DexScreenerPriceResolver
from veltrix.providers.base import ProviderError


class Provider:
    def __init__(self, pairs):
        self.pairs = pairs
        self.calls = []
    def fetch_token_pairs(self, chain, addresses):
        self.calls.append((chain, addresses))
        return self.pairs


def pair(token, price, liquidity):
    return SimpleNamespace(token_address=token, price_usd=price, liquidity_usd=liquidity)


def test_price_resolver_selects_deepest_eligible_pair():
    provider = Provider([
        pair("mint", 0.8, 1000),
        pair("mint", 1.2, 5000),
        pair("other", 10, 100000),
    ])
    resolver = DexScreenerPriceResolver(provider, min_liquidity_usd=500)
    assert resolver("mint") == 1.2
    assert provider.calls == [("solana", ["mint"])]


def test_price_resolver_fails_closed_without_eligible_market_price():
    resolver = DexScreenerPriceResolver(Provider([pair("mint", 0.1, 10)]),
                                        min_liquidity_usd=100)
    try:
        resolver("mint")
    except ProviderError as exc:
        assert "No eligible" in str(exc)
    else:
        raise AssertionError("Missing eligible price must fail closed")
