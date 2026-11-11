from veltrix.solana_devnet_wallet_preflight import (
    DEFAULT_WALLET,
    DEVNET_GENESIS_HASH,
    DEVNET_RPC_URL,
    DevnetPreflightError,
    SolanaDevnetWalletPreflight,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.responses.pop(0))


def test_devnet_wallet_balance_is_verified_read_only():
    assert DEVNET_GENESIS_HASH == "GH7ome3EiwEr7tu9JuTh2dpYWBJK3z69Xm1ZE3MEE6JC"
    session = FakeSession([
        {"jsonrpc": "2.0", "id": 1, "result": DEVNET_GENESIS_HASH},
        {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 260000}},
    ])
    evidence = SolanaDevnetWalletPreflight(session=session).check(DEFAULT_WALLET)
    assert evidence.status == "balance_verified"
    assert evidence.cluster == "devnet"
    assert evidence.balance_lamports == 260000
    assert evidence.balance_sol == "0.000260000"
    assert len(session.calls) == 2
    assert [call[1]["json"]["method"] for call in session.calls] == ["getGenesisHash", "getBalance"]
    assert all(call[0] == DEVNET_RPC_URL for call in session.calls)
    assert evidence.transaction_signed is False
    assert evidence.transaction_submitted is False
    assert evidence.transaction_broadcast is False
    assert evidence.live_trading is False


def test_rejects_wrong_cluster_genesis():
    session = FakeSession([{"jsonrpc": "2.0", "id": 1, "result": "wrong-cluster"}])
    try:
        SolanaDevnetWalletPreflight(session=session).check(DEFAULT_WALLET)
    except DevnetPreflightError as exc:
        assert "Solana Devnet" in str(exc)
    else:
        raise AssertionError("wrong cluster must fail closed")


def test_rejects_invalid_wallet_before_rpc():
    session = FakeSession([])
    try:
        SolanaDevnetWalletPreflight(session=session).check("not-a-wallet")
    except DevnetPreflightError as exc:
        assert "valid Solana public key" in str(exc)
    else:
        raise AssertionError("invalid wallet must fail")
    assert session.calls == []
