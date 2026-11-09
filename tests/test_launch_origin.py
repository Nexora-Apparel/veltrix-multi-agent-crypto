import pytest

from veltrix.launch_origin import (
    SolanaLaunchOriginResolver, EvmLaunchOriginResolver,
)
from veltrix.providers.base import ProviderError


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class SolanaRpcSession:
    def __init__(self, pages, transaction):
        self.pages = list(pages)
        self.transaction = transaction
        self.calls = []
    def post(self, url, json, **kwargs):
        method, params = json["method"], json["params"]
        self.calls.append((method, params))
        if method == "getSignaturesForAddress":
            return Response({"result": self.pages.pop(0)})
        if method == "getTransaction":
            return Response({"result": self.transaction})
        raise AssertionError(method)


def test_solana_origin_requires_oldest_history_and_initialize_mint():
    session = SolanaRpcSession(
        [[
            {"signature": "newer", "slot": 20, "blockTime": 1_700_000_100},
            {"signature": "mint-init", "slot": 10, "blockTime": 1_700_000_000},
        ]],
        {"transaction": {"message": {"instructions": [{
            "program": "spl-token", "parsed": {
                "type": "initializeMint2", "info": {"mint": "MintABC"}
            }
        }]}}},
    )
    resolver = SolanaLaunchOriginResolver("secret", session=session, page_size=10)
    origin = resolver.resolve("MintABC", 120_000, now_ms=1_800_000_000_000)
    assert origin.chain == "solana"
    assert origin.launch_at_ms == 1_700_000_000_000
    assert origin.launch_block == 10
    assert origin.complete
    assert origin.evidence_id


def test_solana_origin_rejects_bounded_history_exhaustion():
    session = SolanaRpcSession(
        [[{"signature": "page-1-oldest", "slot": 1, "blockTime": 1_700_000_000}]],
        {"transaction": {"transaction": {"message": {"instructions": []}}}},
    )
    resolver = SolanaLaunchOriginResolver(
        "secret", session=session, page_size=1, max_pages=1,
    )
    with pytest.raises(ProviderError, match="exceeds bounded history"):
        resolver.resolve("MintABC", 120_000, now_ms=1_800_000_000_000)


def test_solana_origin_rejects_oldest_transaction_without_mint_init():
    session = SolanaRpcSession(
        [[{"signature": "oldest", "slot": 1, "blockTime": 1_700_000_000}]],
        {"transaction": {"message": {"instructions": []}}},
    )
    resolver = SolanaLaunchOriginResolver("secret", session=session)
    with pytest.raises(ProviderError, match="does not prove mint initialization"):
        resolver.resolve("MintABC", 120_000, now_ms=1_800_000_000_000)


class EvmOriginSession:
    def __init__(self):
        self.methods = []
    def post(self, url, json, **kwargs):
        method, params = json["method"], json["params"]
        self.methods.append(method)
        if method == "eth_blockNumber":
            return Response({"result": hex(200)})
        if method == "eth_getCode":
            block = int(params[1], 16)
            return Response({"result": "0x6000" if block >= 150 else "0x"})
        if method == "eth_getBlockByNumber":
            block = int(params[0], 16)
            return Response({"result": {"timestamp": hex(block * 10)}})
        raise AssertionError(method)


def test_evm_origin_finds_first_code_block_and_elapsed_window_end():
    session = EvmOriginSession()
    resolver = EvmLaunchOriginResolver("base", "https://rpc.invalid", session=session)
    origin = resolver.resolve(
        "0x" + "a" * 40, 120_000, now_ms=10_000_000,
    )
    assert origin.chain == "base"
    assert origin.launch_block == 150
    assert origin.launch_at_ms == 1_500_000
    assert origin.window_end_block == 162
    assert origin.window_end_ms == 1_620_000
    assert origin.complete


def test_evm_origin_rejects_address_without_code():
    class EmptyCode(EvmOriginSession):
        def post(self, url, json, **kwargs):
            if json["method"] == "eth_getCode":
                return Response({"result": "0x"})
            return super().post(url, json, **kwargs)
    resolver = EvmLaunchOriginResolver(
        "bsc", "https://rpc.invalid", session=EmptyCode(),
    )
    with pytest.raises(ProviderError, match="No deployed contract code"):
        resolver.resolve("0x" + "b" * 40, 120_000, now_ms=10_000_000)
