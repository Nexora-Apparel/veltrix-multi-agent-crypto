import pytest

from veltrix.providers.alchemy_funding import AlchemyBaseFundingGraphProvider
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


def transfer(sender, recipient, tx, block="0x2", category="external"):
    return {
        "from": sender, "to": recipient, "hash": tx,
        "blockNum": block, "category": category,
        "uniqueId": tx + ":" + category,
    }


def test_base_funding_graph_traces_first_funder_and_intermediary():
    wallet = "0x" + "a" * 40
    intermediary = "0x" + "b" * 40
    root = "0x" + "c" * 40
    def handler(body):
        if body["method"] == "eth_getBlockByNumber":
            return {"result": {"number": "0xa"}}
        if body["method"] == "alchemy_getAssetTransfers":
            query = body["params"][0]
            recipient = query["toAddress"].lower()
            if recipient == wallet:
                rows = [
                    transfer(intermediary, wallet, "tx-first", "0x2"),
                    transfer(root, wallet, "tx-later", "0x3", "erc20"),
                ]
            elif recipient == intermediary:
                rows = [transfer(root, intermediary, "tx-root", "0x1")]
            else:
                rows = []
            return {"result": {"transfers": rows}}
        raise AssertionError(body)
    provider = AlchemyBaseFundingGraphProvider(
        "key", session=Session(handler), clock_ms=lambda: 10_000
    )
    snapshot = provider.fetch_snapshot("0x" + "1" * 40, [wallet])
    edges = {x.wallet_address: x for x in snapshot.relationships}
    assert snapshot.coverage_complete
    assert edges[wallet].funder_address == intermediary
    assert edges[wallet].transaction_id == "tx-first"
    assert edges[intermediary].funder_address == root
    assert root.lower() in snapshot.covered_wallets
    assert snapshot.block_ref == "finalized-block:10"


def test_base_funding_graph_requires_complete_pagination():
    wallet = "0x" + "a" * 40
    def handler(body):
        if body["method"] == "eth_getBlockByNumber":
            return {"result": {"number": "0xa"}}
        return {"result": {"transfers": [], "pageKey": "next"}}
    provider = AlchemyBaseFundingGraphProvider(
        "key", session=Session(handler), max_pages=1
    )
    with pytest.raises(ProviderError, match="pagination bound"):
        provider.fetch_snapshot("token", [wallet])


def test_base_funding_graph_rejects_malformed_and_duplicate_transfers():
    wallet = "0x" + "a" * 40
    sender = "0x" + "b" * 40
    row = transfer(sender, wallet, "tx", "0x2")
    def handler(body):
        if body["method"] == "eth_getBlockByNumber":
            return {"result": {"number": "0xa"}}
        return {"result": {"transfers": [row, row]}}
    provider = AlchemyBaseFundingGraphProvider("key", session=Session(handler))
    with pytest.raises(ProviderError, match="duplicate"):
        provider.fetch_snapshot("token", [wallet])


def test_base_funding_graph_caps_wallet_ancestry():
    wallet = "0x" + "a" * 40
    def handler(body):
        if body["method"] == "eth_getBlockByNumber":
            return {"result": {"number": "0xa"}}
        recipient = body["params"][0]["toAddress"]
        sender = "0x" + "b" * 40
        return {"result": {"transfers": [transfer(sender, recipient, "tx-" + recipient)]}}
    provider = AlchemyBaseFundingGraphProvider(
        "key", session=Session(handler), max_wallets=1
    )
    with pytest.raises(ProviderError, match="wallet cap"):
        provider.fetch_snapshot("token", [wallet])



def test_base_funding_graph_enforces_global_page_budget():
    wallet_a = "0x" + "a" * 40
    wallet_b = "0x" + "b" * 40
    def handler(body):
        if body["method"] == "eth_getBlockByNumber":
            return {"result": {"number": "0xa"}}
        return {"result": {"transfers": []}}
    provider = AlchemyBaseFundingGraphProvider(
        "key", session=Session(handler), max_total_pages=1
    )
    with pytest.raises(ProviderError, match="total page budget"):
        provider.fetch_snapshot("token", [wallet_a, wallet_b])
