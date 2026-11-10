from veltrix.providers.dexscreener import DexScreenerProvider

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WSOL = "So11111111111111111111111111111111111111112"


def test_dexscreener_retains_native_price_and_exact_quote_mint():
    pair = {
        "chainId": "solana",
        "pairAddress": "bonk-wsol-pool",
        "baseToken": {"address": BONK, "symbol": "BONK", "name": "Bonk"},
        "quoteToken": {"address": WSOL, "symbol": "SOL", "name": "Wrapped SOL"},
        "priceUsd": "0.00001",
        "priceNative": "0.0000001",
        "liquidity": {"usd": 250_000},
        "volume": {"h1": 100_000},
        "txns": {"h1": {"buys": 10, "sells": 5}},
    }
    event = DexScreenerProvider._normalize(
        pair, "solana", 1_800_000_000_000, BONK
    )
    assert event is not None
    assert event.quote_token_address == WSOL
    assert event.quote_token_symbol == "SOL"
    assert event.price_native == 0.0000001
