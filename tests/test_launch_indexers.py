from types import SimpleNamespace

from veltrix.providers.launch import (
    FundingTrace, HeliusLaunchIndexer, EvmRpcLaunchIndexer,
    LaunchIndexBatch, LaunchIntelligenceService,
)
from veltrix.launch_bundles import LaunchBundleAnalyzer
from veltrix.safety import SafetyReport


LAUNCH = 1_700_000_000_000
END = LAUNCH + 120_000
NOW = END + 1_000


class Response:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


class HeliusSession:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []
    def post(self, *args, **kwargs):
        self.calls.append(kwargs["json"])
        return Response(self.pages.pop(0))


class FundingResolver:
    provider_id = "funder-indexer"
    def trace_funder(self, chain, wallet_address, before_ms):
        return FundingTrace(
            provider=self.provider_id, chain=chain, wallet_address=wallet_address,
            funder_address="source-wallet", observed_at_ms=before_ms - 1_000,
            evidence_id=f"funding-{wallet_address}", transaction_ids=("funding-tx",),
            coverage_complete=True,
        )


def test_helius_launch_scan_paginates_and_emits_provenance_backed_swap():
    tx = {
        "signature": "sol-swap-1", "parserStatus": "OK",
        "parsed": {
            "blockTime": LAUNCH // 1000 + 30,
            "summary": {"type": "swap"},
            "tokenTransfers": [{
                "mint": "MintABC", "toUserAccount": "buyer-wallet",
                "fromUserAccount": "pool-wallet",
                "rawTokenAmount": 1_250_000, "decimals": 5,
            }],
        },
    }
    session = HeliusSession([
        {"data": [tx], "paginationToken": "next",
         "scan": {"stoppedOn": "budget", "scannedThrough": {"blockTime": END // 1000}}},
        {"data": [], "scan": {"stoppedOn": "windowEnd",
                               "scannedThrough": {"blockTime": END // 1000}}},
    ])
    indexer = HeliusLaunchIndexer(
        "secret", FundingResolver(),
        lambda token, launch: (1_000_000, "historical-mint-index", "slot:123"),
        session=session, clock_ms=lambda: NOW, max_pages=3,
    )
    batch = indexer.index_launch("MintABC", LAUNCH, END)
    assert batch.coverage_complete
    assert batch.pages_scanned == 2
    assert batch.scanned_through_ms == END
    assert batch.total_supply == 1_000_000
    assert batch.observations[0].funder_address == "source-wallet"
    assert batch.observations[0].funder_evidence_id == "funding-buyer-wallet"
    assert session.calls[1]["paginationToken"] == "next"


def test_helius_budget_or_missing_supply_never_claims_complete():
    session = HeliusSession([
        {"data": [], "paginationToken": "still-more",
         "scan": {"stoppedOn": "budget", "scannedThrough": {"blockTime": END // 1000}}}
    ])
    indexer = HeliusLaunchIndexer(
        "secret", FundingResolver(), None, session=session,
        clock_ms=lambda: NOW, max_pages=1,
    )
    batch = indexer.index_launch("MintABC", LAUNCH, END)
    assert not batch.coverage_complete
    assert batch.total_supply is None
    assert any("pagination limit" in reason for reason in batch.rejected)


class EvmRpcSession:
    def __init__(self):
        self.calls = []
    def post(self, url, json, **kwargs):
        self.calls.append((json["method"], json["params"]))
        method, params = json["method"], json["params"]
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            stamp = LAUNCH // 1000 + (number - 100) * 60
            return Response({"result": {"timestamp": hex(stamp)}})
        if method == "eth_call":
            if params[0]["data"] == "0x18160ddd":
                return Response({"result": hex(1_000_000_000)})
            return Response({"result": hex(8)})
        if method == "eth_getLogs":
            return Response({"result": [{
                "transactionHash": "0xswap", "blockNumber": hex(101),
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "0" * 24 + "1" * 40,
                    "0x" + "0" * 24 + "2" * 40,
                ],
                "data": hex(25_000_000),
            }]})
        if method == "eth_getTransactionReceipt":
            return Response({"result": {"status": "0x1", "logs": [{
                "address": "0xquote",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "0" * 24 + "2" * 40,
                    "0x" + "0" * 24 + "3" * 40,
                ],
                "data": hex(100_000_000),
            }]}})
        if method == "eth_getTransactionByHash":
            return Response({"result": {"from": "0x" + "2" * 40, "value": "0x0"}})
        raise AssertionError(method)


def test_evm_indexer_uses_historical_supply_and_receipt_quote_leg():
    session = EvmRpcSession()
    funder = FundingResolver()
    indexer = EvmRpcLaunchIndexer(
        "base", "https://rpc.invalid", funder,
        quote_token_addresses=["0xquote"], session=session,
        clock_ms=lambda: NOW, block_chunk_size=10,
    )
    batch = indexer.index_launch(
        "0xToken", LAUNCH, END, launch_block=100, window_end_block=102,
    )
    assert batch.total_supply == 10.0
    assert batch.supply_block_ref == hex(100)
    assert batch.coverage_complete
    assert len(batch.observations) == 1
    assert batch.observations[0].token_amount == 0.25
    assert batch.observations[0].funder_provider == "funder-indexer"


def test_launch_service_automatically_submits_bundle_to_composite_gate():
    class Indexer:
        provider_id = "helius-launch"
        supported_chains = frozenset({"solana"})
        def index_launch(self, token, start, end, **kwargs):
            event = __import__("veltrix.launch_bundles", fromlist=["LaunchBuyEvidence"]).LaunchBuyEvidence(
                provider="helius-launch", chain="solana", token_address=token,
                wallet_address="buyer", funder_address="source",
                transaction_id="swap-1", token_amount=1.0,
                observed_at_ms=start + 10, funder_provider="funder-indexer",
                funder_evidence_id="funding-proof",
            )
            return LaunchIndexBatch(
                provider="helius-launch", chain="solana", token_address=token,
                launch_at_ms=start, window_end_ms=end, total_supply=100,
                supply_source="helius-launch", supply_block_ref="slot:123",
                supply_observed_at_ms=NOW, observations=(event,),
                coverage_complete=True, scanned_through_ms=end,
                pages_scanned=1, rejected=(), provenance=("scan:windowEnd",),
            )

    class Adapter:
        def validate_composite_safety(self, token, chain, bundle, now):
            assert bundle.launch_bundle.provider == "helius-launch"
            assert bundle.launch_bundle.funder_providers == ("funder-indexer",)
            assert bundle.launch_bundle.coverage_complete
            return SafetyReport(token, True, ("composite",), (), now, composite_verified=True)

    service = LaunchIntelligenceService(
        [Indexer()], LaunchBundleAnalyzer(
            ["helius-launch"], trusted_funder_providers=["funder-indexer"]
        ), Adapter(), ["helius-launch"], ["funder-indexer"],
        launch_window_ms=120_000,
    )
    report, safety = service.evaluate(
        chain="solana", token_address="MintABC", launch_at_ms=LAUNCH,
        now_ms=NOW, liquidity_evidence=None, developer_evidence=None,
    )
    assert report.coverage_complete
    assert report.bundled_supply_pct == 0
    assert safety.passed
    assert safety.composite_verified


def test_untrusted_funder_or_indexer_forces_unknown_bundle_percentage():
    class Indexer:
        provider_id = "untrusted-launch"
        supported_chains = frozenset({"solana"})
        def index_launch(self, token, start, end, **kwargs):
            from veltrix.launch_bundles import LaunchBuyEvidence
            event = LaunchBuyEvidence(
                provider=self.provider_id, chain="solana", token_address=token,
                wallet_address="buyer", funder_address="source",
                transaction_id="swap-1", token_amount=1,
                observed_at_ms=start + 1, funder_provider="bad-funder",
                funder_evidence_id="proof",
            )
            return LaunchIndexBatch(
                provider=self.provider_id, chain="solana", token_address=token,
                launch_at_ms=start, window_end_ms=end, total_supply=100,
                supply_source=self.provider_id, supply_block_ref="slot:1",
                supply_observed_at_ms=NOW, observations=(event,),
                coverage_complete=True, scanned_through_ms=end,
                pages_scanned=1, rejected=(), provenance=(),
            )
    class Adapter:
        def validate_composite_safety(self, token, chain, bundle, now):
            return SafetyReport(token, False, (), ("blocked",), now)
    service = LaunchIntelligenceService(
        [Indexer()], LaunchBundleAnalyzer(
            ["untrusted-launch"], trusted_funder_providers=["funder-indexer"]
        ), Adapter(), ["helius-launch"], ["funder-indexer"],
    )
    report, safety = service.evaluate(
        chain="solana", token_address="MintABC", launch_at_ms=LAUNCH,
        now_ms=NOW, liquidity_evidence=None, developer_evidence=None,
    )
    assert report.bundled_supply_pct is None
    assert not report.coverage_complete
    assert not safety.passed


def test_helius_native_funding_resolver_traces_direct_inbound_sol():
    from veltrix.providers.launch import HeliusNativeFundingResolver
    session = HeliusSession([{
        "data": [{
            "signature": "funding-signature", "parserStatus": "OK",
            "parsed": {
                "blockTime": (END - 5_000) // 1000,
                "nativeTransfers": [{
                    "fromUserAccount": "fund-source",
                    "toUserAccount": "buyer-wallet",
                    "amount": 1_000_000_000,
                }],
            },
        }],
        "scan": {"stoppedOn": "windowEnd", "scannedThrough": {"blockTime": END // 1000}},
    }])
    resolver = HeliusNativeFundingResolver(
        "secret", session=session, clock_ms=lambda: NOW,
        lookback_ms=60_000,
    )
    trace = resolver.trace_funder("solana", "buyer-wallet", END)
    assert trace is not None
    assert trace.funder_address == "fund-source"
    assert trace.provider == "helius-funder"
    assert trace.coverage_complete


class EvmFundingSession:
    def __init__(self):
        self.log_calls = 0
    def post(self, url, json, **kwargs):
        method, params = json["method"], json["params"]
        if method == "eth_blockNumber":
            return Response({"result": hex(256)})
        if method == "eth_getLogs":
            self.log_calls += 1
            if self.log_calls > 1:
                return Response({"result": []})
            return Response({"result": [{
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x" + "0" * 24 + "1" * 40,
                    "0x" + "0" * 24 + "2" * 40,
                ],
                "transactionHash": "0xfund",
                "blockNumber": hex(250),
                "data": hex(1_000_000),
            }]})
        if method == "eth_getBlockByNumber":
            return Response({"result": {"timestamp": hex(END // 1000 - 5)}})
        raise AssertionError(method)


def test_evm_quote_funding_resolver_traces_allowlisted_inbound_asset():
    from veltrix.providers.launch import EvmQuoteFundingResolver
    resolver = EvmQuoteFundingResolver(
        "base", "https://rpc.invalid", ["0xquote"],
        session=EvmFundingSession(), block_chunk_size=10,
        lookback_blocks=10,
    )
    trace = resolver.trace_funder("base", "0x" + "2" * 40, END)
    assert trace is not None
    assert trace.funder_address == "0x" + "1" * 40
    assert trace.provider == "base-quote-funder"
    assert trace.coverage_complete


def test_bsc_uses_same_bounded_historical_rpc_indexer():
    indexer = EvmRpcLaunchIndexer(
        "bsc", "https://bsc-rpc.invalid", FundingResolver(),
        quote_token_addresses=["0xquote"], session=EvmRpcSession(),
        clock_ms=lambda: NOW, block_chunk_size=10,
    )
    batch = indexer.index_launch(
        "0xToken", LAUNCH, END, launch_block=100, window_end_block=102,
    )
    assert batch.chain == "bsc"
    assert batch.provider == "bsc-rpc-launch"
    assert batch.coverage_complete


class HistoricalSupplySession:
    def __init__(self, pages):
        self.pages = list(pages)
    def post(self, url, json, **kwargs):
        method = json.get("method")
        if method == "getTokenSupply":
            return Response({"result": {
                "context": {"slot": 100},
                "value": {"amount": "1000000", "decimals": 2},
            }})
        if method == "getBlockTime":
            return Response({"result": 1_700_000_200})
        if "/parsed-events/scan" in url:
            return Response(self.pages.pop(0))
        raise AssertionError((url, method))


def test_helius_historical_supply_reverses_post_launch_mints_and_burns():
    from veltrix.providers.launch import HeliusHistoricalSupplyResolver
    page = {
        "data": [
            {
                "slot": 101, "summary": {"type": "TOKEN_MINT"},
                "tokenTransfers": [{
                    "mint": "MintABC", "fromUserAccount": None,
                    "toUserAccount": "wallet-a",
                    "rawTokenAmount": 50000, "decimals": 2,
                }],
            },
            {
                "slot": 102, "summary": {"type": "BURN"},
                "tokenTransfers": [{
                    "mint": "MintABC", "fromUserAccount": "wallet-a",
                    "toUserAccount": None,
                    "rawTokenAmount": 10000, "decimals": 2,
                }],
            },
        ],
        "scan": {"stoppedOn": "windowEnd",
                 "scannedThrough": {"blockTime": 1_700_000_200}},
    }
    resolver = HeliusHistoricalSupplyResolver(
        "secret", session=HistoricalSupplySession([page]), max_pages=2,
    )
    supply, source, ref = resolver("MintABC", LAUNCH, launch_slot=10)
    assert supply == 9_600
    assert source == "helius-launch"
    assert "launch-slot:10" in ref
    assert "minted-after:50000" in ref
    assert "burned-after:10000" in ref


def test_helius_historical_supply_withholds_on_incomplete_pagination():
    from veltrix.providers.launch import HeliusHistoricalSupplyResolver
    resolver = HeliusHistoricalSupplyResolver(
        "secret",
        session=HistoricalSupplySession([{
            "data": [], "paginationToken": "more",
            "scan": {"stoppedOn": "budget",
                     "scannedThrough": {"blockTime": 1_700_000_200}},
        }]),
        max_pages=1,
    )
    import pytest
    from veltrix.providers.base import ProviderError
    with pytest.raises(ProviderError, match="pagination limit"):
        resolver("MintABC", LAUNCH, launch_slot=10)


def test_helius_historical_supply_requires_verified_launch_slot():
    from veltrix.providers.launch import HeliusHistoricalSupplyResolver
    import pytest
    from veltrix.providers.base import ProviderError
    resolver = HeliusHistoricalSupplyResolver(
        "secret", session=HistoricalSupplySession([]),
    )
    with pytest.raises(ProviderError, match="launch slot is required"):
        resolver("MintABC", LAUNCH)
