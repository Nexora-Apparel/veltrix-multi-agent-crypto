import pytest

from veltrix.providers.base import MarketEvent
from veltrix.solana_meme.candidates import normalize_candidate
from veltrix.solana_meme.intelligence_worker import build_runtime

MINT = "So11111111111111111111111111111111111111112"


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeProvider:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def fetch_token_pairs(self, chain, token_addresses):
        self.calls.append((chain, list(token_addresses)))
        return [MarketEvent(
            chain="solana", token_address=mint, pair_address=f"pool-{mint[-4:]}",
            symbol="MEME", name="Meme Token", price_usd=0.001,
            liquidity_usd=5000, volume_h1_usd=1200, price_change_m5_pct=1.2,
            observed_at_ms=self.clock(), pair_created_at_ms=9000,
            source="dexscreener", fdv_usd=100_000, market_cap_usd=90_000,
            volume_m5_usd=100, volume_h24_usd=10_000,
            price_change_h1_pct=1.0, price_change_h24_pct=5.0,
            txns_m5_buys=10, txns_m5_sells=5,
            txns_h1_buys=100, txns_h1_sells=50,
            txns_h24_buys=1000, txns_h24_sells=800,
        ) for mint in token_addresses]


def test_runtime_enriches_new_discoveries_then_schedules_refresh(tmp_path):
    clock = Clock(10_000)
    provider = FakeProvider(clock)
    runtime = build_runtime(
        database_path=tmp_path / "runtime.sqlite3",
        poll_interval_seconds=5,
        refresh_interval_ms=1000,
        batch_size=25,
        provider=provider,
        clock_ms=clock,
    )
    runtime.enrichment.launch_store.record_candidate(normalize_candidate(
        {"chain": "solana", "mint": MINT},
        source="pumpportal-new-token",
        observed_at_ms=9000,
    ))

    first = runtime.run_once()
    assert first.enrichment.enriched == 1
    assert first.refresh.discovered == 1
    assert first.refresh.processed == 0
    assert runtime.refresh.market_store.count(MINT) == 1

    clock.value = 11_000
    second = runtime.run_once()
    assert second.refresh.refreshed == 1
    assert runtime.refresh.market_store.count(MINT) == 2
    assert len(provider.calls) == 2


def test_build_runtime_reads_environment_configuration(tmp_path, monkeypatch):
    database = tmp_path / "env.sqlite3"
    monkeypatch.setenv("VELTRIX_SOLANA_DISCOVERY_DB", str(database))
    monkeypatch.setenv("VELTRIX_MARKET_INTELLIGENCE_POLL_SECONDS", "20")
    monkeypatch.setenv("VELTRIX_MARKET_REFRESH_INTERVAL_MS", "90000")
    monkeypatch.setenv("VELTRIX_MARKET_BATCH_SIZE", "30")

    runtime = build_runtime(provider=FakeProvider(Clock(10_000)))

    assert runtime.poll_interval_seconds == 20
    assert runtime.batch_size == 30
    assert runtime.refresh.refresh_interval_ms == 90_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"poll_interval_seconds": 4},
        {"poll_interval_seconds": 3601},
        {"batch_size": 0},
        {"batch_size": 31},
        {"refresh_interval_ms": 0},
    ],
)
def test_build_runtime_rejects_invalid_configuration(tmp_path, kwargs):
    with pytest.raises(ValueError):
        build_runtime(
            database_path=tmp_path / "invalid.sqlite3",
            provider=FakeProvider(Clock(10_000)),
            **kwargs,
        )
