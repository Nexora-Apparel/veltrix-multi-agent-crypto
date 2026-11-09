from veltrix.discovery import DexScreenerDiscovery

class Response:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload
class Session:
    def __init__(self, payload): self.payload = payload
    def get(self, url, timeout):
        assert url.endswith("/token-profiles/latest/v1")
        return Response(self.payload)

def test_discovery_filters_chains_and_deduplicates():
    payload=[
        {"chainId":"solana","tokenAddress":"mint1"},
        {"chainId":"base","tokenAddress":"base1"},
        {"chainId":"ethereum","tokenAddress":"eth1"},
        {"chainId":"solana","tokenAddress":"mint1"},
        {"chainId":"bsc","tokenAddress":""},
    ]
    discovery=DexScreenerDiscovery(session=Session(payload))
    found=discovery.discover(["solana","base","bsc"])
    assert [(x.chain,x.token_address) for x in found] == [("solana","mint1"),("base","base1")]

def test_discovery_returns_empty_for_no_supported_chains():
    assert DexScreenerDiscovery(session=Session([])).discover(["arbitrum"]) == []
