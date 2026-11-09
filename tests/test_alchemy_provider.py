import pytest

from veltrix.providers.alchemy import AlchemyWalletProvider
from veltrix.providers.base import ProviderError
from veltrix.providers.intelligence import VerifiedIntelligenceAdapters


WALLET = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
USDC = "0x3333333333333333333333333333333333333333"


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


def transfer(tx, sender, recipient, contract, value, asset, category="erc20"):
    return {
        "network": "base-mainnet",
        "hash": tx,
        "from": sender,
        "to": recipient,
        "contractAddress": contract,
        "category": category,
        "blockTimestamp": "2026-11-09T05:00:00Z",
        "fungible": {"value": value, "asset": asset, "decimals": 6},
    }


def provider(session, **kwargs):
    return AlchemyWalletProvider(
        "secret", [WALLET], {"base": [USDC], "bsc": [USDC]},
        session=session, clock_ms=lambda: 1_794_199_000_000, **kwargs,
    )


def test_alchemy_requires_inbound_token_and_outbound_allowlisted_stablecoin_same_tx():
    rows = [
        transfer("buy-1", "0xpool", WALLET, TOKEN, 1200, "VEL"),
        transfer("buy-1", WALLET, "0xrouter", USDC, 75, "USDC"),
        # A token deposit without a quote-asset leg is not a buy.
        transfer("deposit", "0xairdrop", WALLET, TOKEN, 500, "VEL"),
    ]
    session = Session([{"transfers": rows}])
    api = provider(session)
    result = api.fetch_wallet_observations("base", [TOKEN], 1_794_198_000_000)
    assert len(result) == 1
    assert result[0].token_address == TOKEN
    assert result[0].notional_usd == 75
    assert result[0].side == "BUY"
    assert result[0].transaction_id == "buy-1"
    assert session.calls[0][0].endswith("/secret/transfers/by-address")
    assert session.calls[0][1]["json"]["networks"] == ["base-mainnet"]
    assert session.calls[0][1]["json"]["direction"] == "both"


def test_alchemy_provider_is_allowlist_gated_by_shared_adapter():
    rows = [
        transfer("buy-2", "0xpool", WALLET, TOKEN, 1, "TOKEN"),
        transfer("buy-2", WALLET, "0xrouter", USDC, 10, "USDC"),
    ]
    api = provider(Session([{"transfers": rows}]))
    denied = VerifiedIntelligenceAdapters().collect_wallet_observations(
        api, "base", [TOKEN], 1_794_198_000_000, 1_794_199_000_000
    )
    assert denied.accepted == ()
    allowed = VerifiedIntelligenceAdapters(
        trusted_wallet_providers=["alchemy"]
    ).collect_wallet_observations(
        api, "base", [TOKEN], 1_794_198_000_000, 1_794_199_000_000
    )
    assert len(allowed.accepted) == 1


def test_alchemy_rejects_wrong_network_and_partial_responses():
    api = provider(Session([{"transfers": [], "error": {
        "partialErrors": [{"network": "base-mainnet", "message": "indexing"}]
    }}]))
    with pytest.raises(ProviderError, match="partial"):
        api.fetch_wallet_observations("base", [TOKEN], 1_794_198_000_000)
    with pytest.raises(ProviderError, match="Base and BSC"):
        api.fetch_wallet_observations("solana", [TOKEN], 1_794_198_000_000)


def test_alchemy_paginates_and_never_treats_unapproved_quote_asset_as_buy():
    session = Session([
        {"transfers": [
            transfer("buy-3", "0xpool", WALLET, TOKEN, 1, "TOKEN"),
            transfer("buy-3", WALLET, "0xrouter", "0x4444444444444444444444444444444444444444", 8, "USDC"),
        ], "pageKey": "next"},
        {"transfers": []},
    ])
    api = provider(session, max_pages=2)
    assert api.fetch_wallet_observations("base", [TOKEN], 1_794_198_000_000) == ()
    assert session.calls[1][1]["json"]["pageKey"] == "next"


def test_alchemy_requires_explicit_quote_token_addresses():
    api = AlchemyWalletProvider("key", [WALLET], session=Session([]))
    assert api.fetch_wallet_observations("base", [TOKEN], 0) == ()
