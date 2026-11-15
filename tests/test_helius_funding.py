import pytest

from veltrix.providers.helius_funding import HeliusSolanaFundingGraphProvider
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
        return Response({"result": self.handler(body["method"], body["params"])})


def transfer(signature, slot, sender, recipient, mint):
    return {
        "signature": signature, "slot": slot, "blockTime": slot * 10,
        "type": "transfer", "fromUserAccount": sender,
        "toUserAccount": recipient, "amount": "1000", "mint": mint,
        "confirmationStatus": "finalized", "transactionIdx": 0,
        "instructionIdx": 0, "innerInstructionIdx": 0,
    }


def test_solana_graph_traces_earliest_native_or_token_transfer_funder():
    wallet, funder, root = "holder-wallet", "first-funder", "root-wallet"
    native_mint = "So11111111111111111111111111111111111111111"
    token_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def handler(method, params):
        if method == "getSlot":
            return 100
        address, query = params
        if address == wallet:
            if query.get("paginationToken") is None:
                return {
                    "data": [transfer("first-token", 10, funder, wallet, token_mint)],
                    "paginationToken": "next",
                }
            return {
                "data": [transfer("later-native", 20, "later-funder", wallet, native_mint)],
                "paginationToken": None,
            }
        if address == funder:
            return {
                "data": [transfer("root-funding", 5, root, funder, native_mint)],
                "paginationToken": None,
            }
        return {"data": [], "paginationToken": None}

    session = Session(handler)
    provider = HeliusSolanaFundingGraphProvider(
        "test-key", session=session, clock_ms=lambda: 100_000
    )
    snapshot = provider.fetch_snapshot("token-mint", [wallet])
    edges = {row.wallet_address: row for row in snapshot.relationships}
    assert snapshot.coverage_complete is True
    assert edges[wallet].funder_address == funder
    assert edges[wallet].transaction_id == "first-token"
    assert edges[funder].funder_address == root
    assert set(snapshot.covered_wallets) == {wallet, funder, root}
    assert snapshot.block_ref == "helius-finalized-slot:100;pages:4"
    requests = [call for call in session.calls if call["method"] == "getTransfersByAddress"]
    assert requests[0]["params"][1]["direction"] == "in"
    assert requests[0]["params"][1]["sortOrder"] == "asc"
    assert requests[0]["params"][1]["filters"]["slot"]["lte"] == 100


def test_solana_graph_exhausts_transfer_pagination_before_claiming_coverage():
    def handler(method, params):
        if method == "getSlot":
            return 100
        return {"data": [], "paginationToken": "next"}
    provider = HeliusSolanaFundingGraphProvider(
        "key", session=Session(handler), clock_ms=lambda: 100_000, max_pages=1
    )
    with pytest.raises(ProviderError, match="pagination bound"):
        provider.fetch_snapshot("mint", ["holder"])


def test_solana_graph_rejects_unfinalized_or_malformed_transfer_rows():
    def handler(method, params):
        if method == "getSlot":
            return 100
        address, _ = params
        return {
            "data": [{
                **transfer("pending", 10, "funder", address, "mint"),
                "confirmationStatus": "confirmed",
            }],
            "paginationToken": None,
        }
    provider = HeliusSolanaFundingGraphProvider(
        "key", session=Session(handler), clock_ms=lambda: 100_000
    )
    with pytest.raises(ProviderError, match="finalized slot"):
        provider.fetch_snapshot("mint", ["holder"])


def test_solana_graph_enforces_wallet_and_global_page_budgets():
    def empty_history(method, params):
        if method == "getSlot":
            return 100
        return {"data": [], "paginationToken": None}
    provider = HeliusSolanaFundingGraphProvider(
        "key", session=Session(empty_history), clock_ms=lambda: 100_000,
        max_wallets=1,
    )
    with pytest.raises(ProviderError, match="wallet cap"):
        provider.fetch_snapshot("mint", ["holder-a", "holder-b"])

    provider = HeliusSolanaFundingGraphProvider(
        "key", session=Session(empty_history), clock_ms=lambda: 100_000,
        max_total_pages=1,
    )
    with pytest.raises(ProviderError, match="total transfer-page budget"):
        provider.fetch_snapshot("mint", ["holder-a", "holder-b"])
