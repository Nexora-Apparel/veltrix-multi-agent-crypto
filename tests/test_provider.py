from veltrix.providers.dexscreener import DexScreenerProvider
from veltrix.providers.base import ProviderError

class Response:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload
class Session:
    def __init__(self, payload): self.payload, self.urls = payload, []
    def get(self, url, timeout): self.urls.append(url); return Response(self.payload)

def test_normalizes_supported_chain_pair():
    pair={"chainId":"base","pairAddress":"pair1","baseToken":{"address":"token1","symbol":"TOK"},"priceUsd":"1.25","liquidity":{"usd":10000},"volume":{"h1":5000},"priceChange":{"m5":2.5},"pairCreatedAt":1700000000000}
    event=DexScreenerProvider(session=Session([pair])).fetch_token_pairs("base",["token1"])[0]
    assert event.token_address=="token1" and event.price_usd==1.25 and event.is_fresh(event.observed_at_ms)
def test_rejects_unsupported_chain():
    try: DexScreenerProvider(session=Session([])).fetch_token_pairs("unknown",["x"])
    except ProviderError: return
    assert False, "unsupported chain must fail closed"
def test_ignores_other_chain_pairs():
    assert DexScreenerProvider(session=Session([{"chainId":"solana"}])).fetch_token_pairs("base",["x"])==[]
