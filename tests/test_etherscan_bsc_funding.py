import pytest

from veltrix.providers.base import ProviderError
from veltrix.providers.etherscan_bsc_funding import EtherscanBscFundingGraphProvider


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
        self.get_calls = []
        self.post_calls = []
    def post(self, url, **kwargs):
        self.post_calls.append(kwargs["json"])
        return Response({"result": {"number": "0x64"}})
    def get(self, url, **kwargs):
        self.get_calls.append(kwargs["params"])
        return Response(self.handler(kwargs["params"]))


def row(sender, recipient, block, tx_hash, *, action, value="100", index="0"):
    result = {
        "from": sender, "to": recipient, "value": value,
        "blockNumber": str(block), "timeStamp": str(block * 10),
        "hash": tx_hash, "transactionIndex": "0",
    }
    if action in {"txlist", "txlistinternal"}:
        result["isError"] = "0"
    if action == "txlist":
        result["txreceipt_status"] = "1"
    if action == "txlistinternal":
        result["traceId"] = index
    if action == "tokentx":
        result["logIndex"] = index
        result["contractAddress"] = "0x" + "a" * 40
    return result


def no_rows():
    return {"status": "0", "message": "No transactions found", "result": "No transactions found"}


def test_bsc_graph_combines_external_internal_and_erc20_histories():
    holder, funder, root = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
    later = "0x" + "44" * 20

    def handler(params):
        address, action = params["address"], params["action"]
        if address == holder:
            if action == "txlist":
                return {"status": "1", "message": "OK",
                        "result": [row(later, holder, 30, "normal-later", action=action)]}
            if action == "txlistinternal":
                return {"status": "1", "message": "OK",
                        "result": [row(later, holder, 20, "internal-later", action=action, index="0_0")]}
            if action == "tokentx":
                return {"status": "1", "message": "OK",
                        "result": [row(funder, holder, 10, "first-token", action=action, index="2")]}
        if address == funder and action == "txlist":
            return {"status": "1", "message": "OK",
                    "result": [row(root, funder, 5, "root-funding", action=action)]}
        return no_rows()

    session = Session(handler)
    provider = EtherscanBscFundingGraphProvider(
        "api-key", "https://bsc-rpc.example", session=session,
        clock_ms=lambda: 100_000, page_size=10,
    )
    snapshot = provider.fetch_snapshot("0x" + "aa" * 20, [holder])
    edges = {edge.wallet_address: edge for edge in snapshot.relationships}
    assert snapshot.coverage_complete
    assert edges[holder].funder_address == funder
    assert edges[holder].transaction_id == "first-token"
    assert edges[funder].funder_address == root
    assert set(snapshot.covered_wallets) == {holder, funder, root}
    assert snapshot.block_ref == "finalized-block:100;pages:9"
    assert all(call["chainid"] == "56" for call in session.get_calls)
    assert {call["action"] for call in session.get_calls} == {
        "txlist", "txlistinternal", "tokentx"
    }


def test_bsc_graph_fails_closed_on_explorer_errors_and_history_caps():
    def api_error(params):
        return {"status": "0", "message": "NOTOK", "result": "Invalid API key"}
    provider = EtherscanBscFundingGraphProvider(
        "bad-key", "https://bsc-rpc.example", session=Session(api_error),
        clock_ms=lambda: 100_000,
    )
    with pytest.raises(ProviderError, match="unavailable or incomplete"):
        provider.fetch_snapshot("token", ["holder"])

    def full_page(params):
        action = params["action"]
        return {"status": "1", "message": "OK",
                "result": [row("funder", params["address"], 1, "tx", action=action)]}
    provider = EtherscanBscFundingGraphProvider(
        "key", "https://bsc-rpc.example", session=Session(full_page),
        clock_ms=lambda: 100_000, page_size=1,
    )
    provider.MAX_RECORDS_PER_ACTION = 1
    with pytest.raises(ProviderError, match="10,000-record ceiling"):
        provider.fetch_snapshot("token", ["holder"])


def test_bsc_graph_rejects_pagination_truncation_and_future_blocks():
    def full_page(params):
        action = params["action"]
        return {"status": "1", "message": "OK",
                "result": [row("funder", params["address"], 1, "tx", action=action)]}
    provider = EtherscanBscFundingGraphProvider(
        "key", "https://bsc-rpc.example", session=Session(full_page),
        clock_ms=lambda: 100_000, page_size=1, max_pages=1,
    )
    with pytest.raises(ProviderError, match="page budget"):
        provider.fetch_snapshot("token", ["holder"])

    def future(params):
        action = params["action"]
        return {"status": "1", "message": "OK",
                "result": [row("funder", params["address"], 101, "tx", action=action)]}
    provider = EtherscanBscFundingGraphProvider(
        "key", "https://bsc-rpc.example", session=Session(future),
        clock_ms=lambda: 100_000, page_size=10,
    )
    with pytest.raises(ProviderError, match="provenance is invalid"):
        provider.fetch_snapshot("token", ["holder"])


def test_bsc_graph_enforces_wallet_and_total_page_budgets():
    provider = EtherscanBscFundingGraphProvider(
        "key", "https://bsc-rpc.example", session=Session(lambda params: no_rows()),
        clock_ms=lambda: 100_000, max_wallets=1,
    )
    with pytest.raises(ProviderError, match="wallet cap"):
        provider.fetch_snapshot("token", ["wallet-a", "wallet-b"])

    provider = EtherscanBscFundingGraphProvider(
        "key", "https://bsc-rpc.example", session=Session(lambda params: no_rows()),
        clock_ms=lambda: 100_000, max_total_pages=2,
    )
    with pytest.raises(ProviderError, match="total page budget"):
        provider.fetch_snapshot("token", ["wallet-a"])
