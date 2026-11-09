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
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payload)


def test_helius_normalizes_only_inbound_target_mint_swaps():
    tx = {
        "type": "SWAP", "signature": "sig-1", "timestamp": 1_000,
        "tokenTransfers": [
            {"mint": "mint-a", "toUserAccount": "wallet-a",
             "fromUserAccount": "pool", "tokenAmount": 25.0},
            {"mint": "other-mint", "toUserAccount": "wallet-a",
             "fromUserAccount": "pool", "tokenAmount": 100.0},
            {"mint": "mint-a", "toUserAccount": "pool",
             "fromUserAccount": "wallet-a", "tokenAmount": 5.0},
        ],
    }
    session = Session([tx])
    provider = HeliusWalletProvider(
        "test-key", ["wallet-a"], lambda mint: 2.0,
        session=session, clock_ms=lambda: 1_001_000,
    )
    rows = provider.fetch_wallet_observations("solana", ["mint-a"], 900_000)
    assert len(rows) == 1
    assert rows[0].notional_usd == 50.0
    assert rows[0].side == "BUY"
    assert rows[0].transaction_id == "sig-1"
    assert session.calls[0][1]["params"]["api-key"] == "test-key"


def test_helius_rejects_failed_old_and_non_swap_transactions():
    payload = [
        {"type": "SWAP", "signature": "failed", "timestamp": 1000,
         "transactionError": {"InstructionError": [0, "x"]},
         "tokenTransfers": [{"mint": "mint-a", "toUserAccount": "wallet-a",
                             "fromUserAccount": "pool", "tokenAmount": 1}]},
        {"type": "TRANSFER", "signature": "transfer", "timestamp": 1000,
         "tokenTransfers": [{"mint": "mint-a", "toUserAccount": "wallet-a",
                             "fromUserAccount": "pool", "tokenAmount": 1}]},
        {"type": "SWAP", "signature": "old", "timestamp": 10,
         "tokenTransfers": [{"mint": "mint-a", "toUserAccount": "wallet-a",
                             "fromUserAccount": "pool", "tokenAmount": 1}]},
    ]
    provider = HeliusWalletProvider(
        "key", ["wallet-a"], lambda mint: 1,
        session=Session(payload), clock_ms=lambda: 2_000_000,
    )
    assert provider.fetch_wallet_observations("solana", ["mint-a"], 900_000) == ()


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
