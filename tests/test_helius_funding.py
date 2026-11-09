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
        self.calls.append(kwargs["json"])
        return Response(self.handler(kwargs["json"]))


def envelope(signature, stamp, transfers):
    return {
        "signature": signature,
        "parserStatus": "OK",
        "parsed": {
            "signature": signature,
            "blockTime": stamp,
            "nativeTransfers": transfers,
        },
    }


def test_solana_graph_traces_earliest_inbound_funder_and_intermediary():
    wallet = "holder-wallet"
    funder = "first-funder"
    root = "root-wallet"

    def handler(body):
        address = body["address"]
        if address == wallet:
            return {
                "data": [
                    envelope("later", 80, [{"fromUserAccount": "later-funder",
                                            "toUserAccount": wallet, "amount": 5}]),
                    envelope("first", 40, [{"fromUserAccount": funder,
                                            "toUserAccount": wallet, "amount": 3}]),
                ],
                "paginationToken": None,
                "scan": {"stoppedOn": "windowEnd", "scannedThrough": {"blockTime": 100}},
            }
        if address == funder:
            return {
                "data": [envelope("root-tx", 20, [{"fromUserAccount": root,
                                                   "toUserAccount": funder, "amount": 2}])],
                "paginationToken": None,
                "scan": {"stoppedOn": "windowEnd", "scannedThrough": {"blockTime": 100}},
            }
        return {
            "data": [],
            "paginationToken": None,
            "scan": {"stoppedOn": "windowEnd", "scannedThrough": {"blockTime": 100}},
        }

    provider = HeliusSolanaFundingGraphProvider(
        "test-key", session=Session(handler), clock_ms=lambda: 100_000
    )
    snapshot = provider.fetch_snapshot("mint", [wallet])
    edges = {row.wallet_address: row for row in snapshot.relationships}
    assert snapshot.coverage_complete is True
    assert edges[wallet].funder_address == funder
    assert edges[wallet].transaction_id == "first"
    assert edges[funder].funder_address == root
    assert set(snapshot.covered_wallets) == {wallet, funder, root}
    assert snapshot.coverage_end_ms == 100_000


def test_solana_graph_exhausts_pagination_before_claiming_coverage():
    wallet = "holder"
    calls = {"n": 0}

    def handler(body):
        calls["n"] += 1
        cursor = body.get("paginationToken")
        return {
            "data": [],
            "paginationToken": "next" if not cursor else None,
            "scan": {"stoppedOn": "windowEnd", "scannedThrough": {"blockTime": 100}},
        }

    provider = HeliusSolanaFundingGraphProvider(
        "key", session=Session(handler), clock_ms=lambda: 100_000,
        max_pages=2,
    )
    snapshot = provider.fetch_snapshot("mint", [wallet])
    assert snapshot.coverage_complete
    assert calls["n"] == 2


def test_solana_graph_rejects_pagination_truncation_and_parser_errors():
    def truncated(body):
        return {
            "data": [], "paginationToken": "next",
            "scan": {"stoppedOn": "pageLimit", "scannedThrough": {"blockTime": 100}},
        }
    provider = HeliusSolanaFundingGraphProvider(
        "key", session=Session(truncated), clock_ms=lambda: 100_000, max_pages=1
    )
    with pytest.raises(ProviderError, match="pagination bound"):
        provider.fetch_snapshot("mint", ["holder"])

    def parser_error(body):
        return {
            "data": [{"signature": "bad", "parserStatus": "ERROR", "parsed": None}],
            "paginationToken": None,
            "scan": {"stoppedOn": "windowEnd", "scannedThrough": {"blockTime": 100}},
        }
    provider = HeliusSolanaFundingGraphProvider(
        "key", session=Session(parser_error), clock_ms=lambda: 100_000
    )
    with pytest.raises(ProviderError, match="unparsed"):
        provider.fetch_snapshot("mint", ["holder"])


def test_solana_graph_enforces_intermediary_wallet_cap():
    def handler(body):
        wallet = body["address"]
        return {
            "data": [envelope("funding", 50, [{"fromUserAccount": "next-wallet",
                                                "toUserAccount": wallet, "amount": 1}])],
            "paginationToken": None,
            "scan": {"stoppedOn": "windowEnd", "scannedThrough": {"blockTime": 100}},
        }
    provider = HeliusSolanaFundingGraphProvider(
        "key", session=Session(handler), clock_ms=lambda: 100_000, max_wallets=1
    )
    with pytest.raises(ProviderError, match="wallet cap"):
        provider.fetch_snapshot("mint", ["holder"])
