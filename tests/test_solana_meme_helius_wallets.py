from veltrix.providers.helius import HeliusWalletProvider
from veltrix.providers.intelligence import SmartWalletObservation, VerifiedIntelligenceAdapters
from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.helius_wallets import HeliusConsensusWalletEventSource
from veltrix.solana_meme.market_data import SolanaMarketData
from veltrix.solana_meme.market_store import MarketSnapshotStore

MINT = "So11111111111111111111111111111111111111112"
WALLET_A = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WALLET_B = "Vote111111111111111111111111111111111111111"


class MarketProvider:
    def fetch_token_pairs(self, chain, token_addresses):
        return [MarketEvent(
            chain="solana", token_address=MINT, pair_address="pool",
            symbol="MEME", name="Meme", price_usd=0.002,
            liquidity_usd=50_000, volume_h1_usd=10_000,
            price_change_m5_pct=1.0, observed_at_ms=9_900,
            pair_created_at_ms=1_000, source="dexscreener",
        )]


def setup_store(tmp_path):
    store = MarketSnapshotStore(tmp_path / "shared.sqlite3")
    snapshot = SolanaMarketData(
        MarketProvider(), clock_ms=lambda: 10_000
    ).fetch(MINT, now_ms=10_000)
    store.record(snapshot)
    return store


def test_helius_source_uses_latest_market_price_and_normalizes_trusted_buys(tmp_path):
    store = setup_store(tmp_path)
    provider = HeliusWalletProvider(
        "test-key", (WALLET_A, WALLET_B), lambda _mint: 0.0,
        clock_ms=lambda: 10_000,
    )
    provider.fetch_wallet_observations = lambda chain, mints, since: (
        SmartWalletObservation(
            provider="helius", chain="solana", token_address=MINT,
            wallet_address=WALLET_A, side="BUY", notional_usd=150,
            transaction_id="signature-a", observed_at_ms=9_950,
        ),
        SmartWalletObservation(
            provider="helius", chain="solana", token_address=MINT,
            wallet_address=WALLET_B, side="BUY", notional_usd=175,
            transaction_id="signature-b", observed_at_ms=9_960,
        ),
    )
    adapters = VerifiedIntelligenceAdapters(
        trusted_wallet_providers=("helius",), max_wallet_age_ms=300_000
    )
    source = HeliusConsensusWalletEventSource(
        provider, adapters, store, clock_ms=lambda: 10_000,
    )

    events = source(MINT)

    assert provider.price_usd_resolver(MINT) == 0.002
    assert len(events) == 2
    assert {event.wallet_address for event in events} == {WALLET_A, WALLET_B}
    assert all(event.source == "helius" and event.chain == "solana" for event in events)


def test_helius_source_requires_explicit_provider_trust(tmp_path):
    provider = HeliusWalletProvider("key", (WALLET_A,), lambda _mint: 1.0)
    adapters = VerifiedIntelligenceAdapters(trusted_wallet_providers=())
    try:
        HeliusConsensusWalletEventSource(
            provider, adapters, setup_store(tmp_path), clock_ms=lambda: 10_000,
        )
    except ValueError as exc:
        assert "explicitly trusted" in str(exc)
    else:
        raise AssertionError("untrusted Helius must not be wired")
