import pytest

from veltrix.developer_concentration import FundingGraphSnapshot, FundingRelationship
from veltrix.launch_origin import LaunchOrigin
from veltrix.providers.helius_launch_index import HeliusSolanaLaunchIndexProvider
from veltrix.providers.solana_mint import SolanaMintAuthoritySnapshot, TOKEN_PROGRAM_ID
from veltrix.providers.base import ProviderError

MINT = "So11111111111111111111111111111111111111112"
NOW = 2_000_000


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class Session:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []
    def post(self, url, **kwargs):
        self.calls.append(kwargs["json"])
        return Response(self.pages.pop(0))


class Origin:
    def resolve(self, mint, window, *, now_ms):
        return LaunchOrigin(
            "solana", mint, NOW - window, 100, None, NOW,
            "helius-launch-origin", "origin-proof", True,
        )


class MintProvider:
    def fetch_snapshot(self, mint):
        return SolanaMintAuthoritySnapshot(
            mint, "solana-finalized-mint-account", "mint-proof", NOW - 10,
            100, TOKEN_PROGRAM_ID, None, None, 1_000_000_000, 6,
            (), (), True, "a" * 64,
        )


class FundingProvider:
    provider_id = "helius-solana-funding-graph"
    def __init__(self, complete=True):
        self.complete = complete
        self.wallets = []
    def fetch_snapshot(self, mint, wallets):
        self.wallets.extend(wallets)
        edges = tuple(
            FundingRelationship(w, "shared-funder", f"fund-{w}", NOW - 10, f"edge-{w}")
            for w in wallets
        ) + (FundingRelationship("shared-funder", None, None, NOW - 10, "edge-root"),)
        return FundingGraphSnapshot(
            self.provider_id, "solana", mint, NOW - 10, "funding-proof",
            "helius-scan", 0, NOW - 5,
            tuple(sorted(set(wallets) | {"shared-funder"})), edges,
            self.complete,
        )


def provider(session, funding=None, *, max_pages=2):
    item = HeliusSolanaLaunchIndexProvider(
        "test-key", "https://rpc.example", funding or FundingProvider(),
        session=session, launch_window_ms=120_000, max_scan_pages=max_pages,
        clock_ms=lambda: NOW,
    )
    item.origin_resolver = Origin()
    item.mint_provider = MintProvider()
    return item


def buy(signature="buy-1", wallet="wallet-a", amount="100.0"):
    return {
        "signature": signature,
        "parserStatus": "OK",
        "parsed": {
            "signature": signature,
            "blockTime": (NOW - 60_000) // 1000,
            "instructions": [{
                "programId": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                "instructionName": "buy",
            }],
            "tokenTransfers": [{
                "mint": MINT, "toUserAccount": wallet, "tokenAmount": amount,
            }],
        },
    }


def scan_page(data, *, cursor=None, stopped="windowEnd"):
    return {
        "data": data, "paginationToken": cursor,
        "scan": {"stoppedOn": stopped, "scannedThrough": {"blockTime": NOW // 1000}},
    }


def test_provider_normalizes_launch_buy_and_resolves_funder():
    funding = FundingProvider()
    session = Session([scan_page([buy()])])
    result = provider(session, funding).fetch_launch_snapshot("solana", MINT)
    assert result.provider == "helius-solana-launch-index"
    assert result.total_supply == 1000.0
    assert result.coverage.source_complete is True
    assert result.coverage.pages_completed == 1
    assert len(result.observations) == 1
    row = result.observations[0]
    assert row.wallet_address == "wallet-a"
    assert row.funder_address == "shared-funder"
    assert row.funder_provider == funding.provider_id
    assert row.funder_evidence_id == "funding-proof"


def test_provider_withholds_complete_coverage_when_scan_pagination_is_truncated():
    session = Session([
        scan_page([], cursor="next", stopped="budget"),
        scan_page([], cursor="next-2", stopped="budget"),
    ])
    result = provider(session, max_pages=2).fetch_launch_snapshot("solana", MINT)
    assert result.coverage.source_complete is False
    assert result.coverage.pages_expected > result.coverage.pages_completed


def test_provider_withholds_coverage_when_buyer_funding_is_incomplete():
    funding = FundingProvider(complete=False)
    session = Session([scan_page([buy()])])
    result = provider(session, funding).fetch_launch_snapshot("solana", MINT)
    assert result.coverage.source_complete is False
    assert result.observations[0].funder_address == "shared-funder"


def test_provider_rejects_unparsed_or_non_buy_scan_rows():
    session = Session([scan_page([{"signature": "bad", "parserStatus": "ERROR", "parsed": None}])])
    with pytest.raises(ProviderError, match="unparsed"):
        provider(session).fetch_launch_snapshot("solana", MINT)

    wrong = buy()
    wrong["parsed"]["instructions"][0]["instructionName"] = "sell"
    session = Session([scan_page([wrong])])
    with pytest.raises(ProviderError, match="outside the Pump.fun buy filter"):
        provider(session).fetch_launch_snapshot("solana", MINT)


def test_provider_rejects_missing_buyer_or_token_transfer_provenance():
    row = buy()
    row["parsed"]["tokenTransfers"][0].pop("toUserAccount")
    session = Session([scan_page([row])])
    with pytest.raises(ProviderError, match="buyer wallet"):
        provider(session).fetch_launch_snapshot("solana", MINT)
