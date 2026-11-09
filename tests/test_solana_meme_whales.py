import pytest

from veltrix.whales import WalletBuyEvent
from veltrix.solana_meme.solana_whales import SolanaWhaleFlowAggregator

TOKEN = "So11111111111111111111111111111111111111112"
WALLET_A = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET_B = "Vote111111111111111111111111111111111111111"
WALLET_C = "11111111111111111111111111111111"


def test_only_allowlisted_fresh_solana_wallets_form_cluster():
    events = [
        WalletBuyEvent("solana", TOKEN, WALLET_A, 9900, 20, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_B, 9950, 30, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_C, 9950, 999, "helius"),
        WalletBuyEvent("solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", WALLET_A, 1, 100, "helius"),
        WalletBuyEvent("base", "not-an-address", "wallet", 9950, 999, "indexer"),
    ]
    clusters = SolanaWhaleFlowAggregator(
        [WALLET_A, WALLET_B], window_ms=1000, min_buy_usd=10
    ).aggregate(events, 10000)

    assert len(clusters) == 1
    assert clusters[0].token_address == TOKEN
    assert clusters[0].wallet_addresses == tuple(sorted((WALLET_A, WALLET_B)))
    assert clusters[0].total_notional_usd == 50


def test_multiple_buys_from_one_wallet_count_once_but_notional_is_summed():
    events = [
        WalletBuyEvent("solana", TOKEN, WALLET_A, 9900, 20, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_A, 9950, 30, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_B, 9975, 10, "helius"),
    ]
    cluster = SolanaWhaleFlowAggregator(
        [WALLET_A, WALLET_B], min_buy_usd=10
    ).aggregate(events, 10000)[0]
    assert len(cluster.wallet_addresses) == 2
    assert cluster.total_notional_usd == 60
    assert cluster.first_seen_ms == 9900
    assert cluster.last_seen_ms == 9975


def test_exact_duplicate_observation_is_not_double_counted():
    event = WalletBuyEvent("solana", TOKEN, WALLET_A, 9900, 20, "helius")
    events = [event, event, WalletBuyEvent("solana", TOKEN, WALLET_B, 9950, 30, "helius")]
    cluster = SolanaWhaleFlowAggregator([WALLET_A, WALLET_B], min_buy_usd=10).aggregate(events, 10000)[0]
    assert cluster.total_notional_usd == 50


@pytest.mark.parametrize(
    "event",
    [
        WalletBuyEvent("solana", "bad-mint", WALLET_A, 9900, 20, "helius"),
        WalletBuyEvent("solana", TOKEN, "bad-wallet", 9900, 20, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_A, True, 20, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_A, 9900, float("nan"), "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_A, 9900, 20, ""),
    ],
)
def test_malformed_solana_wallet_observations_are_ignored(event):
    assert SolanaWhaleFlowAggregator([WALLET_A, WALLET_B]).aggregate([event], 10000) == []


def test_single_wallet_and_empty_allowlist_do_not_create_clusters():
    event = WalletBuyEvent("solana", TOKEN, WALLET_A, 9900, 20, "helius")
    assert SolanaWhaleFlowAggregator([WALLET_A]).aggregate([event], 10000) == []
    assert SolanaWhaleFlowAggregator().aggregate([event], 10000) == []


def test_aggregator_rejects_invalid_allowlist_and_limits():
    with pytest.raises(ValueError):
        SolanaWhaleFlowAggregator(["not-a-wallet"])
    with pytest.raises(ValueError):
        SolanaWhaleFlowAggregator(window_ms=0)
    with pytest.raises(ValueError):
        SolanaWhaleFlowAggregator(min_buy_usd=float("inf"))
