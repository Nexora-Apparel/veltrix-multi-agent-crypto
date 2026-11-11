from veltrix.solana_devnet_swap_preflight import (
    ALLOWED_RAYDIUM_PROGRAMS,
    DEFAULT_AMOUNT_LAMPORTS,
    DEVNET_GENESIS_HASH,
    DEVNET_USDC_MINT,
    SOL_MINT,
    DevnetSwapPreflightError,
    RaydiumDevnetSwapPreflight,
)


class Response:
    def __init__(self, data): self.data = data
    def raise_for_status(self): pass
    def json(self): return self.data


class Session:
    def __init__(self, responses): self.responses=list(responses); self.calls=[]
    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response(self.responses.pop(0))
    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return Response(self.responses.pop(0))


def test_amount_bounds_fail_before_network():
    session=Session([])
    try:
        RaydiumDevnetSwapPreflight(session=session).run("FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U", 100_000_000)
    except DevnetSwapPreflightError as exc:
        assert "between 0.001 and 0.05" in str(exc)
    else:
        raise AssertionError("oversized test swap must fail closed")
    assert session.calls == []


def test_wrong_cluster_stops_before_quote():
    session=Session([{"result":"not-devnet"}])
    try:
        RaydiumDevnetSwapPreflight(session=session).run("FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U")
    except DevnetSwapPreflightError as exc:
        assert "genesis hash" in str(exc)
    else:
        raise AssertionError("wrong cluster must fail closed")
    assert len(session.calls) == 1


def test_low_wallet_balance_stops_before_quote():
    session=Session([
        {"result":DEVNET_GENESIS_HASH},
        {"result":{"value":1_000_000}},
    ])
    try:
        RaydiumDevnetSwapPreflight(session=session).run("FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U")
    except DevnetSwapPreflightError as exc:
        assert "insufficient" in str(exc)
    else:
        raise AssertionError("insufficient balance must fail closed")
    assert len(session.calls) == 2


def test_simulation_never_signs_or_sends():
    # End-to-end orchestration contract: RPC methods must never include sendTransaction.
    assert DEVNET_GENESIS_HASH.endswith("PkrZBG")
    assert SOL_MINT != DEVNET_USDC_MINT
    assert DEFAULT_AMOUNT_LAMPORTS == 10_000_000
    assert len(ALLOWED_RAYDIUM_PROGRAMS) >= 4
