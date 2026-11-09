import pytest

from veltrix.providers.base import ProviderError
from veltrix.providers.intelligence import SmartWalletObservation
from veltrix.solana_meme.helius_wallet_events import (
    HeliusWalletEventBatchProvider,
    StoredMarketSpotPriceResolver,
)

MINT_A = "So11111111111111111111111111111111111111112"
MINT_B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET_A = "Vote111111111111111111111111111111111111111"
WALLET_B = "11111111111111111111111111111111"


class FakeMarketStore:
    def latest(self, mint):
        return {"retrieved_at_ms": 9_900, "price_usd": 0.25}


class FakeHelius:
    provider_id = "helius"

    def __init__(self, wallets, observations):
        self.tracked_wallets = tuple(wallets)
        self.observations = observations
        self.calls = []

    def fetch_wallet_observations(self, chain, token_addresses, since_ms):
        self.calls.append((chain, tuple(token_addresses), since_ms))
        return self.observations


def observation(wallet, mint=MINT_A, *, observed=9_950, side="BUY", notional=150):
    return SmartWalletObservation(
        provider="helius", chain="solana", token_address=mint,
        wallet_address=wallet, side=side, notional_usd=notional,
        transaction_id=f"signature-{wallet}", observed_at_ms=observed,
    )


def build_provider(fake, **kwargs):
    return HeliusWalletEventBatchProvider(
        api_key="test-key",
        trusted_wallets=fake.tracked_wallets,
        trusted_provider_ids=("helius",),
        market_store=FakeMarketStore(),
        clock_ms=lambda: 10_000,
        provider=fake,
        **kwargs,
    )


def test_helius_wallet_provider_batches_candidate_mints_in_one_scan():
    fake = FakeHelius(
        (WALLET_A, WALLET_B),
        (
            observation(WALLET_A, MINT_A),
            observation(WALLET_B, MINT_A, observed=9_960),
            observation(WALLET_A, MINT_B),
        ),
    )
    source = build_provider(fake)

    events = source((MINT_A, MINT_B), now_ms=10_000)

    assert fake.calls == [("solana", (MINT_A, MINT_B), 0)]
    assert len(events) == 3
    assert {event.token_address for event in events} == {MINT_A, MINT_B}
    assert all(event.source == "helius" for event in events)


def test_helius_wallet_provider_rejects_stale_future_and_non_buy_observations():
    fake = FakeHelius(
        (WALLET_A, WALLET_B),
        (
            observation(WALLET_A, observed=1),
            observation(WALLET_B, observed=10_001),
            observation(WALLET_A, side="SELL"),
        ),
    )
    source = build_provider(fake, max_wallet_age_ms=300)
    assert source((MINT_A,), now_ms=10_000) == ()


def test_helius_wallet_provider_requires_explicit_provider_trust():
    fake = FakeHelius((WALLET_A,), ())
    with pytest.raises(ValueError, match="explicitly listed"):
        HeliusWalletEventBatchProvider(
            api_key="test-key",
            trusted_wallets=(WALLET_A,),
            trusted_provider_ids=(),
            market_store=FakeMarketStore(),
            provider=fake,
        )


def test_helius_wallet_provider_rejects_allowlist_over_scan_limit():
    fake = FakeHelius((WALLET_A, WALLET_B), ())
    with pytest.raises(ValueError, match="exceeds the configured Helius scan limit"):
        build_provider(fake, max_tracked_wallets=1)

def test_stored_market_price_resolver_rejects_stale_and_invalid_prices():
    class Store:
        def __init__(self, row):
            self.row = row

        def latest(self, mint):
            return self.row

    fresh = StoredMarketSpotPriceResolver(
        Store({"retrieved_at_ms": 9_950, "price_usd": 0.25}),
        clock_ms=lambda: 10_000,
        max_age_ms=100,
    )
    assert fresh(MINT_A) == 0.25

    stale = StoredMarketSpotPriceResolver(
        Store({"retrieved_at_ms": 8_000, "price_usd": 0.25}),
        clock_ms=lambda: 10_000,
        max_age_ms=100,
    )
    with pytest.raises(ProviderError, match="stale"):
        stale(MINT_A)

    invalid = StoredMarketSpotPriceResolver(
        Store({"retrieved_at_ms": 9_950, "price_usd": float("nan")}),
        clock_ms=lambda: 10_000,
        max_age_ms=100,
    )
    with pytest.raises(ProviderError, match="invalid"):
        invalid(MINT_A)
