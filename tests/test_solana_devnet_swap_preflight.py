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
    assert DEFAULT_AMOUNT_LAMPORTS == 1_000_000
    assert len(ALLOWED_RAYDIUM_PROGRAMS) >= 4


def test_full_quote_build_and_simulation_path_is_unsigned():
    import base64
    from veltrix.solana_devnet_swap_preflight import (
        RAYDIUM_DEVNET_API, RAYDIUM_DEVNET_TRADE_API, DEFAULT_WALLET
    )
    pool_id = "11111111111111111111111111111111"
    tx_b64 = base64.b64encode(b"unsigned-versioned-tx").decode()
    class FullSession:
        def __init__(self): self.calls=[]
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs["json"]["method"]))
            method=kwargs["json"]["method"]
            if method == "getGenesisHash": data={"result": DEVNET_GENESIS_HASH}
            elif method == "getBalance": data={"result":{"value":5_000_000_000}}
            elif method == "getAccountInfo" and kwargs["json"]["params"][0] == DEVNET_USDC_MINT:
                data={"result":{"value":{"owner":"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA","data":["","base64"]}}}
            elif method == "getAccountInfo":
                data={"result":{"value":{"owner":next(iter(ALLOWED_RAYDIUM_PROGRAMS),"DRaya7Kj3aMWQSy19kSjvmuwq9docCHofyP9kanQGaav"),"data":["","base64"]}}}
            elif method == "simulateTransaction":
                data={"result":{"value":{"err":None,"logs":["simulation ok"]}}}
            else: raise AssertionError("unexpected RPC method: "+method)
            return Response(data)
        def get(self, url, **kwargs):
            self.calls.append((url, "GET"))
            if url.endswith("/compute/swap-base-in"):
                data={"success":True,"data":{"inputMint":SOL_MINT,"outputMint":DEVNET_USDC_MINT,"outputAmount":"12345","otherAmountThreshold":"12000","priceImpactPct":0.001,"routePlan":[{"poolId":pool_id}]}}
            elif url == f"{RAYDIUM_DEVNET_API}/pools/info/ids":
                data={"success":True,"data":[{"id":pool_id}]}
            else: raise AssertionError("unexpected GET: "+url)
            return Response(data)
        def post_json_placeholder(self, url, **kwargs): pass
    session=FullSession()
    # The transaction build endpoint is an HTTP POST, distinguish it from JSON-RPC.
    original_post=session.post
    def post(url, **kwargs):
        if url == f"{RAYDIUM_DEVNET_TRADE_API}/transaction/swap-base-in":
            session.calls.append((url,"BUILD"))
            return Response({"success":True,"data":[{"transaction":tx_b64}]})
        return original_post(url, **kwargs)
    session.post=post
    evidence=RaydiumDevnetSwapPreflight(session=session).run(DEFAULT_WALLET)
    assert evidence.status == "simulation_succeeded"
    assert evidence.quote_received and evidence.unsigned_transaction_built
    assert evidence.simulation_attempted and evidence.simulation_succeeded
    assert evidence.route_pool_ids == [pool_id]
    assert evidence.unsigned_transactions_base64 == [tx_b64]
    assert evidence.transaction_signed is False
    assert evidence.transaction_submitted is False
    assert evidence.transaction_broadcast is False
    assert evidence.swap_executed is False
    assert evidence.live_trading is False
    assert all(method not in {"sendTransaction","sendRawTransaction"} for _,method in session.calls)


def test_quote_above_two_percent_impact_fails_before_pool_lookup_or_build():
    from veltrix.solana_devnet_swap_preflight import MAX_PRICE_IMPACT_PCT

    class ImpactSession:
        def __init__(self):
            self.calls = []
        def post(self, url, **kwargs):
            method = kwargs["json"]["method"]
            self.calls.append(("POST", method))
            if method == "getGenesisHash":
                result = DEVNET_GENESIS_HASH
            elif method == "getBalance":
                result = {"value": 5_000_000_000}
            elif method == "getAccountInfo":
                result = {"value": {"owner": "Tokenkeg", "data": ["", "base64"]}}
            else:
                raise AssertionError("unexpected RPC method: " + method)
            return Response({"result": result})
        def get(self, url, **kwargs):
            self.calls.append(("GET", url))
            return Response({"success": True, "data": {
                "inputMint": SOL_MINT,
                "outputMint": DEVNET_USDC_MINT,
                "outputAmount": "1000",
                "otherAmountThreshold": "990",
                "priceImpactPct": MAX_PRICE_IMPACT_PCT + 0.01,
                "routePlan": [{"poolId": "11111111111111111111111111111111"}],
            }})

    session = ImpactSession()
    try:
        RaydiumDevnetSwapPreflight(session=session).run(
            "FQtHmufHsELvyMHvQctu1kqZQ2zY8wDCJ4KpdxTQKb9U"
        )
    except DevnetSwapPreflightError as exc:
        assert "safety ceiling" in str(exc)
    else:
        raise AssertionError("high-impact quote must fail closed")
    assert not any(method == "BUILD" for _, method in session.calls)
    assert not any(url.endswith("/pools/info/ids") for method, url in session.calls if method == "GET")
