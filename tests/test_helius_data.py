from veltrix.providers.helius import HeliusWalletProvider


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
    def post(self, *args, **kwargs):
        return Response(self.payload)


def test_helius_wallet_provider_unwraps_documented_parsed_record_and_raw_amount():
    now = 1_700_000_100_000
    provider = HeliusWalletProvider(
        "secret", tracked_wallets=["wallet-a"],
        price_usd_resolver=lambda mint: 2.0,
        session=Session({"data": [{
            "signature": "swap-signature", "parserStatus": "OK",
            "parsed": {
                "blockTime": now // 1000,
                "summary": {"type": "swap"},
                "tokenTransfers": [{
                    "mint": "MintABC", "fromUserAccount": "pool",
                    "toUserAccount": "wallet-a",
                    "rawTokenAmount": 1_000_000, "decimals": 5,
                }],
            },
        }], "scan": {"stoppedOn": "windowEnd"}}),
        clock_ms=lambda: now,
    )
    rows = provider.fetch_wallet_observations("solana", ["MintABC"], now - 10_000)
    assert len(rows) == 1
    assert rows[0].token_address == "MintABC"
    assert rows[0].transaction_id == "swap-signature"
    assert rows[0].notional_usd == 20.0
