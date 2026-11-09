from veltrix.launch_bundles import LaunchBuyEvidence, LaunchBundleAnalyzer


LAUNCH = 1_000_000


def buy(wallet, funder, amount=10, *, tx=None, at=LAUNCH + 1,
        provider="indexer", chain="solana", token="MintABC"):
    return LaunchBuyEvidence(
        provider=provider, chain=chain, token_address=token,
        wallet_address=wallet, funder_address=funder,
        transaction_id=tx or f"sig-{wallet}", token_amount=amount,
        observed_at_ms=at,
    )


def analyzer():
    return LaunchBundleAnalyzer(["indexer"], launch_window_ms=120_000)


def test_shared_funder_cluster_calculates_only_cluster_supply():
    report = analyzer().analyze(
        provider="indexer", chain="solana", token_address="MintABC",
        launch_at_ms=LAUNCH, total_supply=1_000, observations=[
            buy("wallet-A", "funder-X", 100),
            buy("wallet-B", "funder-X", 150),
            buy("wallet-C", "funder-Y", 50),
        ], coverage_complete=True, now_ms=LAUNCH + 10_000,
    )
    assert report.bundled_supply_pct == 25
    assert report.shared_funder_clusters == 1
    assert report.participating_wallets == 2
    assert report.accepted_transactions == 3


def test_incomplete_coverage_never_reports_zero():
    report = analyzer().analyze(
        provider="indexer", chain="solana", token_address="MintABC",
        launch_at_ms=LAUNCH, total_supply=1_000, observations=[],
        coverage_complete=False, now_ms=LAUNCH + 10_000,
    )
    assert report.bundled_supply_pct is None
    assert report.shared_funder_clusters == 0


def test_complete_coverage_with_no_cluster_can_report_zero():
    report = analyzer().analyze(
        provider="indexer", chain="solana", token_address="MintABC",
        launch_at_ms=LAUNCH, total_supply=1_000,
        observations=[buy("wallet-A", "funder-X", 100),
                      buy("wallet-B", "funder-Y", 100)],
        coverage_complete=True, now_ms=LAUNCH + 10_000,
    )
    assert report.bundled_supply_pct == 0
    assert report.shared_funder_clusters == 0


def test_shared_funder_is_not_enough_without_distinct_wallets():
    report = analyzer().analyze(
        provider="indexer", chain="solana", token_address="MintABC",
        launch_at_ms=LAUNCH, total_supply=1_000,
        observations=[buy("wallet-A", "funder-X", 100, tx="sig-1"),
                      buy("wallet-A", "funder-X", 100, tx="sig-2")],
        coverage_complete=True, now_ms=LAUNCH + 10_000,
    )
    assert report.bundled_supply_pct == 0
    assert report.participating_wallets == 0


def test_wrong_token_chain_and_duplicate_transactions_are_rejected():
    report = analyzer().analyze(
        provider="indexer", chain="base", token_address="0xToken",
        launch_at_ms=LAUNCH, total_supply=1_000, observations=[
            buy("0xA", "0xF", 100, chain="base", token="0xOther"),
            buy("0xA", "0xF", 100, chain="bsc", token="0xToken"),
            buy("0xA", "0xF", 100, chain="base", token="0xToken", tx="0xTX"),
            buy("0xB", "0xF", 100, chain="base", token="0xToken", tx="0xtx"),
        ], coverage_complete=True, now_ms=LAUNCH + 10_000,
    )
    assert report.accepted_transactions == 1
    assert report.bundled_supply_pct is None
    assert len(report.rejected) == 4


def test_stale_future_and_invalid_amounts_are_rejected():
    report = analyzer().analyze(
        provider="indexer", chain="solana", token_address="MintABC",
        launch_at_ms=LAUNCH, total_supply=1_000, observations=[
            buy("wallet-A", "funder-X", 100, at=LAUNCH - 1),
            buy("wallet-B", "funder-X", 100, at=LAUNCH + 200_000),
            buy("wallet-C", "funder-X", float("nan")),
        ], coverage_complete=True, now_ms=LAUNCH + 10_000,
    )
    assert report.accepted_transactions == 0
    assert report.bundled_supply_pct is None
    assert len(report.rejected) == 3


def test_unknown_or_overflowing_supply_withholds_percentage():
    for supply in (None, 0, -1, 100):
        report = analyzer().analyze(
            provider="indexer", chain="solana", token_address="MintABC",
            launch_at_ms=LAUNCH, total_supply=supply,
            observations=[buy("wallet-A", "funder-X", 100),
                          buy("wallet-B", "funder-X", 100)],
            coverage_complete=True, now_ms=LAUNCH + 10_000,
        )
        assert report.bundled_supply_pct is None


def test_untrusted_provider_cannot_create_bundle_report():
    report = LaunchBundleAnalyzer().analyze(
        provider="indexer", chain="solana", token_address="MintABC",
        launch_at_ms=LAUNCH, total_supply=1_000,
        observations=[buy("wallet-A", "funder-X"), buy("wallet-B", "funder-X")],
        coverage_complete=True, now_ms=LAUNCH + 10_000,
    )
    assert report.bundled_supply_pct is None
    assert report.accepted_transactions == 0
    assert "untrusted provider or invalid chain/token/launch scope" in report.rejected
