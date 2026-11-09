import pytest

from veltrix.whales import WalletBuyEvent, WhaleFlowAggregator

TOKEN = "So11111111111111111111111111111111111111112"
WALLET_A = "Vote111111111111111111111111111111111111111"
WALLET_B = "Stake11111111111111111111111111111111111111"
WALLET_C = "11111111111111111111111111111111"


def test_only_allowlisted_fresh_wallets_form_solana_cluster():
    events = [
        WalletBuyEvent("solana", TOKEN, WALLET_A, 9900, 20, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_B, 9950, 30, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_C, 9950, 999, "helius"),
        WalletBuyEvent("solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", WALLET_A, 1, 100, "helius"),
    ]
    clusters = WhaleFlowAggregator(
        [WALLET_A, WALLET_B], window_ms=1000, min_buy_usd=10
    ).aggregate(events, 10000)

    assert len(clusters) == 1
    assert clusters[0].token_address == TOKEN
    assert clusters[0].wallet_addresses == (WALLET_A, WALLET_B)
    assert clusters[0].total_notional_usd == 50


def test_multiple_buys_from_one_wallet_count_once_but_notional_is_summed():
    events = [
        WalletBuyEvent("solana", TOKEN, WALLET_A, 9900, 20, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_A, 9950, 30, "helius"),
        WalletBuyEvent("solana", TOKEN, WALLET_B, 9975, 10, "helius"),
    ]
    cluster = WhaleFlowAggregator([WALLET_A, WALLET_B]).aggregate(events, 10000)[0]
    assert len(cluster.wallet_addresses) == 2
    assert cluster.total_notional_usd == 60
    assert cluster.first_seen_ms == 9900
    assert cluster.last_seen_ms == 9975


def test_exact_duplicate_observation_is_not_double_counted():
    event = WalletBuyEvent("solana", TOKEN, WALLET_A, 9900, 20, "helius")
    events = [event, event, WalletBuyEvent("solana", TOKEN, WALLET_B, 9950, 30, "helius")]
    cluster = WhaleFlowAggregator([WALLET_A, WALLET_B]).aggregate(events, 10000)[0]
    assert cluster.total_notional_usd == 50


def test_single_wallet_does_not_count_as_cluster():
    event = WalletBuyEvent("solana", TOKEN, WALLET_A, 100, 25, "helius")
    assert WhaleFlowAggregator([WALLET_A]).aggregate([event], 100) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chain": "base", "token_address": TOKEN, "wallet_address": WALLET_A, "observed_at_ms": 100, "notional_usd": 10, "source": "feed"},
        {"chain": "solana", "token_address": "bad", "wallet_address": WALLET_A, "observed_at_ms": 100, "notional_usd": 10, "source": "feed"},
        {"chain": "solana", "token_address": TOKEN, "wallet_address": "bad", "observed_at_ms": 100, "notional_usd": 10, "source": "feed"},
        {"chain": "solana", "token_address": TOKEN, "wallet_address": WALLET_A, "observed_at_ms": True, "notional_usd": 10, "source": "feed"},
        {"chain": "solana", "token_address": TOKEN, "wallet_address": WALLET_A, "observed_at_ms": 100, "notional_usd": float("nan"), "source": "feed"},
        {"chain": "solana", "token_address": TOKEN, "wallet_address": WALLET_A, "observed_at_ms": 100, "notional_usd": 10, "source": ""},
    ],
)
def test_wallet_events_reject_malformed_evidence(kwargs):
    with pytest.raises(ValueError):
        WalletBuyEvent(**kwargs)


def test_aggregator_rejects_invalid_allowlist_and_limits():
    with pytest.raises(ValueError):
        WhaleFlowAggregator(["not-a-wallet"])
    with pytest.raises(ValueError):
        WhaleFlowAggregator(window_ms=0)
    with pytest.raises(ValueError):
        WhaleFlowAggregator(min_buy_usd=float("inf"))
