import base64
import pytest

from veltrix.providers.holder_inventory import (
    EVM_ZERO_ADDRESS, EvmHolderInventoryProvider, SolanaHolderInventoryProvider,
    SOLANA_TOKEN_PROGRAMS, TRANSFER_TOPIC,
)
from veltrix.providers.base import ProviderError


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class Session:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
    def post(self, url, **kwargs):
        body = kwargs["json"]
        self.calls.append(body)
        return Response(self.handler(body))


def test_solana_holder_adapter_combines_token_programs_and_reconciles_supply():
    mint = "Mint111"
    def handler(body):
        method = body["method"]
        if method == "getProgramAccounts":
            program = body["params"][0]
            owner = "owner-a" if program == SOLANA_TOKEN_PROGRAMS[0] else "owner-b"
            return {"result": {"context": {"slot": 500}, "value": [{
                "pubkey": "acct-" + owner,
                "account": {"data": {"parsed": {"info": {
                    "mint": mint, "owner": owner,
                    "tokenAmount": {"amount": "40" if owner == "owner-a" else "60"},
                }}}},
            }]}}
        if method == "getTokenSupply":
            return {"result": {"context": {"slot": 500},
                               "value": {"amount": "100", "decimals": 6}}}
        raise AssertionError(method)
    session = Session(handler)
    provider = SolanaHolderInventoryProvider(
        "https://rpc.invalid", session=session, clock_ms=lambda: 1000
    )
    snapshot = provider.fetch_snapshot(mint)
    assert snapshot.coverage_complete
    assert snapshot.total_supply_raw == 100
    assert {x.wallet_address: x.balance_raw for x in snapshot.holders} == {
        "owner-a": 40, "owner-b": 60
    }
    assert snapshot.block_ref == "finalized-slot:500"


def test_solana_holder_adapter_rejects_supply_context_mismatch():
    def handler(body):
        if body["method"] == "getProgramAccounts":
            return {"result": {"context": {"slot": 500}, "value": []}}
        return {"result": {"context": {"slot": 501},
                           "value": {"amount": "1", "decimals": 0}}}
    provider = SolanaHolderInventoryProvider(
        "https://rpc.invalid", session=Session(handler)
    )
    with pytest.raises(ProviderError, match="same slot"):
        provider.fetch_snapshot("Mint111")


def test_solana_holder_adapter_rejects_at_safety_cap():
    def handler(body):
        if body["method"] == "getProgramAccounts":
            return {"result": {"context": {"slot": 500}, "value": [
                {"pubkey": "account-" + body["params"][0], "account": {"data": {"parsed": {"info": {
                    "mint": "Mint111", "owner": "owner", "tokenAmount": {"amount": "1"}
                }}}}}
            ]}}
        return {"result": {"context": {"slot": 500},
                           "value": {"amount": "1", "decimals": 0}}}
    provider = SolanaHolderInventoryProvider(
        "https://rpc.invalid", session=Session(handler), max_accounts=1
    )
    with pytest.raises(ProviderError, match="safety cap"):
        provider.fetch_snapshot("Mint111")


def _topic(address):
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def test_evm_holder_adapter_reconstructs_and_reconciles_transfer_history():
    token = "0x" + "1" * 40
    alice, bob = "0x" + "a" * 40, "0x" + "b" * 40
    zero = "0x" + "0" * 40
    def handler(body):
        method, params = body["method"], body["params"]
        if method == "eth_getBlockByNumber":
            return {"result": {"number": "0xa"}}
        if method == "eth_getLogs":
            return {"result": [
                {"address": token, "blockNumber": "0x2", "transactionHash": "0x" + "1" * 64, "logIndex": "0x0", "topics": [TRANSFER_TOPIC, _topic(zero), _topic(alice)]},
                {"address": token, "blockNumber": "0x3", "transactionHash": "0x" + "2" * 64, "logIndex": "0x0", "topics": [TRANSFER_TOPIC, _topic(alice), _topic(bob)]},
            ]}
        if method == "eth_call":
            data = params[0]["data"]
            if data == "0x313ce567":
                return {"result": "0x" + hex(6)[2:].rjust(64, "0")}
            if data == "0x18160ddd":
                return {"result": "0x" + hex(100)[2:].rjust(64, "0")}
            target = "0x" + data[-40:]
            amount = 60 if target == alice else 40 if target == bob else 0
            return {"result": "0x" + hex(amount)[2:].rjust(64, "0")}
        raise AssertionError(method)
    provider = EvmHolderInventoryProvider(
        "base", "https://rpc.invalid", session=Session(handler),
        block_chunk_size=10, clock_ms=lambda: 2000,
    )
    snapshot = provider.fetch_snapshot(token, 1)
    assert snapshot.coverage_complete
    assert snapshot.total_supply_raw == 100
    assert {x.wallet_address: x.balance_raw for x in snapshot.holders} == {
        alice: 60, bob: 40
    }
    assert snapshot.block_ref == "finalized-block:10"


def test_evm_holder_adapter_rejects_unreconciled_supply():
    token = "0x" + "1" * 40
    def handler(body):
        if body["method"] == "eth_getBlockByNumber":
            return {"result": {"number": "0x2"}}
        if body["method"] == "eth_getLogs":
            return {"result": []}
        if body["method"] == "eth_call":
            data = body["params"][0]["data"]
            if data == "0x313ce567":
                return {"result": "0x" + hex(18)[2:].rjust(64, "0")}
            return {"result": "0x" + hex(100)[2:].rjust(64, "0")}
        raise AssertionError(body)
    provider = EvmHolderInventoryProvider(
        "bsc", "https://rpc.invalid", session=Session(handler)
    )
    with pytest.raises(ProviderError, match="do not reconcile"):
        provider.fetch_snapshot(token, 1)


def test_evm_holder_adapter_rejects_log_cap_and_non_finalized_rpc():
    token = "0x" + "1" * 40
    def handler(body):
        if body["method"] == "eth_getBlockByNumber":
            return {"result": {"number": "0x2"}}
        if body["method"] == "eth_getLogs":
            return {"result": [
                {"topics": [TRANSFER_TOPIC, _topic(EVM_ZERO_ADDRESS), _topic("0x" + "a" * 40)]}
            ]}
        raise AssertionError(body)
    def handler_with_two_logs(body):
        if body["method"] == "eth_getBlockByNumber":
            return {"result": {"number": "0x2"}}
        if body["method"] == "eth_getLogs":
            topic = _topic(EVM_ZERO_ADDRESS)
            other = _topic("0x" + "b" * 40)
            row = {"address": token, "blockNumber": "0x2", "transactionHash": "0x" + "1" * 64, "logIndex": "0x0", "topics": [TRANSFER_TOPIC, topic, other]}
            duplicate = dict(row, transactionHash="0x" + "2" * 64)
            return {"result": [row, duplicate]}
        raise AssertionError(body)
    provider = EvmHolderInventoryProvider(
        "base", "https://rpc.invalid", session=Session(handler_with_two_logs), max_logs=1
    )
    with pytest.raises(ProviderError, match="log cap"):
        provider.fetch_snapshot(token, 1)
