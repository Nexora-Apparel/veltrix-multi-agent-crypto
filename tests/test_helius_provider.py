from veltrix.providers.helius import HeliusWalletProvider
from veltrix.providers.base import ProviderError


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class Session:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payloads.pop(0))


def parsed_swap(signature, mint="mint-a", wallet="wallet-a", block_time=1000):
    return {
        "signature": signature,
        "blockTime": block_time,
        "summary": {"type": "swap", "parsedData": {"output_mint": mint}},
        "tokenTransfers": [
            {"mint": mint, "toUserAccount": wallet,
             "fromUserAccount": "pool", "tokenAmount": "25"},
        ],
    }


def test_helius_parsed_events_normalizes_inbound_target_swap():
    session = Session([{"data": [parsed_swap("sig-1")]}])
    provider = HeliusWalletProvider(
        "test-key", ["wallet-a"], lambda mint: 2.0,
        session=session, clock_ms=lambda: 1_001_000,
    )
    rows = provider.fetch_wallet_observations("solana", ["mint-a"], 900_000)
    assert len(rows) == 1
    assert rows[0].notional_usd == 50.0
    assert rows[0].side == "BUY"
    assert rows[0].transaction_id == "sig-1"
    url, call = session.calls[0]
    assert url.endswith("/v1/parsed-events/scan")
    assert call["params"]["api-key"] == "test-key"
    assert call["json"]["match"]["mints"] == [{"mint": "mint-a"}]
    assert call["json"]["tokenAccounts"] == "balanceChanged"


def test_helius_rejects_non_swap_outbound_old_and_bad_amount():
    data = [
        {"signature": "transfer", "blockTime": 1000,
         "summary": {"type": "transfer"},
         "tokenTransfers": [{"mint": "mint-a", "toUserAccount": "wallet-a",
                             "fromUserAccount": "pool", "tokenAmount": 1}]},
        {"signature": "outbound", "blockTime": 1000,
         "summary": {"type": "swap"},
         "tokenTransfers": [{"mint": "mint-a", "toUserAccount": "pool",
                             "fromUserAccount": "wallet-a", "tokenAmount": 1}]},
        parsed_swap("old", block_time=10),
        {"signature": "bad-amount", "blockTime": 1000,
         "summary": {"type": "swap"},
         "tokenTransfers": [{"mint": "mint-a", "toUserAccount": "wallet-a",
                             "fromUserAccount": "pool", "tokenAmount": "not-number"}]},
    ]
    provider = HeliusWalletProvider(
        "key", ["wallet-a"], lambda mint: 1,
        session=Session([{"data": data}]), clock_ms=lambda: 2_000_000,
    )
    assert provider.fetch_wallet_observations("solana", ["mint-a"], 900_000) == ()


def test_helius_parsed_events_paginates_with_cursor():
    session = Session([
        {"data": [parsed_swap("page-1")], "paginationToken": "cursor-2"},
        {"data": [parsed_swap("page-2")], "paginationToken": None},
    ])
    provider = HeliusWalletProvider(
        "key", ["wallet-a"], lambda mint: 1,
        session=session, clock_ms=lambda: 2_000_000, max_pages=2,
    )
    rows = provider.fetch_wallet_observations("solana", ["mint-a"], 0)
    assert [row.transaction_id for row in rows] == ["page-1", "page-2"]
    assert session.calls[1][1]["json"]["paginationToken"] == "cursor-2"


def test_helius_requires_price_resolver_and_solana_chain():
    try:
        HeliusWalletProvider("key", ["wallet-a"])
    except ValueError:
        pass
    else:
        raise AssertionError("Price resolver must be explicit")
    provider = HeliusWalletProvider("key", ["wallet-a"], lambda mint: 1)
    try:
        provider.fetch_wallet_observations("base", ["mint-a"], 0)
    except ProviderError as exc:
        assert "Solana only" in str(exc)
    else:
        raise AssertionError("Unsupported chain must fail")


def test_helius_rejects_malformed_api_response():
    provider = HeliusWalletProvider(
        "key", ["wallet-a"], lambda mint: 1, session=Session([{"bad": []}])
    )
    try:
        provider.fetch_wallet_observations("solana", ["mint-a"], 0)
    except ProviderError as exc:
        assert "unexpected Parsed Events" in str(exc)
    else:
        raise AssertionError("Malformed response must fail closed")
