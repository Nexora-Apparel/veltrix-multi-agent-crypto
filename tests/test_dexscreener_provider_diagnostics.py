from veltrix.providers.dexscreener import DexScreenerProvider


MINT = "So11111111111111111111111111111111111111112"
QUOTE = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, payload):
        self.payload = payload

    def get(self, url, *, timeout):
        return Response(self.payload)


def test_dexscreener_reports_raw_and_normalized_pair_counts():
    provider = DexScreenerProvider(
        session=Session([{
            "chainId": "solana",
            "dexId": "raydium",
            "pairAddress": "pool-1",
            "baseToken": {"address": MINT, "symbol": "SOL"},
            "quoteToken": {"address": QUOTE, "symbol": "USDC"},
            "priceUsd": "100",
            "liquidity": {"usd": 1000},
            "volume": {"h1": 500},
        }]),
        clock_ms=lambda: 1000,
    )

    events = provider.fetch_token_pairs("solana", [MINT])

    assert len(events) == 1
    assert provider.last_fetch_diagnostics == {
        "requested_mints": 1,
        "request_batches": 1,
        "response_pairs": 1,
        "matching_token_pairs": 1,
        "normalized_events": 1,
        "normalization_rejections": 0,
        "provider_errors": 0,
    }


def test_dexscreener_distinguishes_empty_response_from_normalization_failure():
    provider = DexScreenerProvider(
        session=Session([]),
        clock_ms=lambda: 1000,
    )

    events = provider.fetch_token_pairs("solana", [MINT])

    assert events == []
    assert provider.last_fetch_diagnostics["response_pairs"] == 0
    assert provider.last_fetch_diagnostics["normalized_events"] == 0
    assert provider.last_fetch_diagnostics["normalization_rejections"] == 0
